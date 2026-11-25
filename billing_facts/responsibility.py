"""Lease interpretation and responsibility assignment."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .sources import effective_leases
from .timeutil import dec, iso, parse_ts
from .timeline import issue_id

Row = dict[str, Any]


def _lease_interval(body: dict[str, Any]) -> tuple[int, int]:
    return parse_ts(body["start"]), parse_ts(body["end"])


def assign_responsibility(
    timeline: dict[str, Any], rows: list[Row], before_seq: int | None = None
) -> dict[str, Any]:
    leases, disputes = effective_leases(rows, before_seq)
    by_meter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for body in leases.values():
        start, end = _lease_interval(body)
        enriched = dict(body)
        enriched["_start_ts"] = start
        enriched["_end_ts"] = end
        by_meter[body["meter"]].append(enriched)

    intervals: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    # Lease identity revision conflicts are preserved as source issues.
    for identity, bodies in sorted(disputes.items()):
        meters = sorted({b.get("meter", "?") for b in bodies})
        for meter in meters:
            issues.append(
                {
                    "issue_id": issue_id("lease_revision_conflict", identity, meter),
                    "kind": "lease_revision_conflict",
                    "blocking": True,
                    "meter": meter,
                    "source_ids": [identity],
                    "message": "same lease revision has contradictory payloads",
                    "bodies": bodies,
                }
            )

    for seg in timeline["segments"]:
        meter = seg["meter"]
        s, e = int(seg["start_ts"]), int(seg["end_ts"])
        candidates = [
            lease
            for lease in by_meter.get(meter, [])
            if max(s, lease["_start_ts"]) < min(e, lease["_end_ts"])
        ]
        coords = {s, e}
        for lease in candidates:
            coords.add(max(s, lease["_start_ts"]))
            coords.add(min(e, lease["_end_ts"]))
        coords = sorted(x for x in coords if s <= x <= e)

        for a, b in zip(coords, coords[1:]):
            if a >= b:
                continue
            active = [l for l in candidates if l["_start_ts"] <= a and l["_end_ts"] > a]
            duration = b - a
            tick_offset = a - s
            tick_a = int(seg["tick_start"]) + tick_offset
            tick_b = tick_a + duration
            base = {
                "responsibility_id": f"resp:{seg['fact_id'].split(':', 2)[2]}:{a}-{b}",
                "fact_id": seg["fact_id"],
                "meter": meter,
                "boot": seg["boot"],
                "start_ts": a,
                "end_ts": b,
                "start": iso(a),
                "end": iso(b),
                "seconds": duration,
                "tick_start": tick_a,
                "tick_end": tick_b,
                "state": seg["state"],
                "effective_use": bool(seg["effective_use"]),
                "source_id": seg["source_id"],
                "source_ids": seg["source_ids"],
                "source_coverage": seg.get("source_coverage", []),
            }

            if not active:
                item = {
                    **base,
                    "status": "unclaimed",
                    "allocations": [],
                    "unclaimed_fraction": Decimal("1"),
                    "unclaimed_seconds": dec(duration),
                    "lease_ids": [],
                    "contracts": [],
                }
                intervals.append(item)
                if seg["effective_use"]:
                    issues.append(_balance_issue("unclaimed", item, blocking=False))
                continue

            dedicated = [l for l in active if not l.get("group")]
            groups = {str(l.get("group")) for l in active if l.get("group")}
            if len(active) == 1 and dedicated:
                lease = active[0]
                allocations = [
                    {
                        "contract": str(lease["contract"]),
                        "lease_id": str(lease["id"]),
                        "group": None,
                        "share_percent": Decimal("100"),
                        "fraction": Decimal("1"),
                        "allocated_seconds": dec(duration),
                    }
                ]
                item = {
                    **base,
                    "status": "allocated",
                    "allocations": allocations,
                    "lease_ids": [str(lease["id"])],
                    "contracts": [str(lease["contract"])],
                    "share_sum_percent": Decimal("100"),
                    "unclaimed_fraction": Decimal("0"),
                    "unclaimed_seconds": Decimal("0"),
                }
                intervals.append(item)
                continue

            if dedicated or len(groups) > 1:
                item = {
                    **base,
                    "status": "lease_conflict",
                    "allocations": [],
                    "unclaimed_fraction": Decimal("0"),
                    "unclaimed_seconds": Decimal("0"),
                    "lease_ids": sorted(str(l["id"]) for l in active),
                    "contracts": sorted({str(l["contract"]) for l in active}),
                    "groups": sorted(groups),
                    "dedicated": [str(l["id"]) for l in dedicated],
                }
                intervals.append(item)
                issues.append(
                    _balance_issue(
                        "lease_conflict",
                        item,
                        blocking=bool(seg["effective_use"]),
                        message="valid dedicated/group leases collide; usage is preserved but not chosen",
                    )
                )
                continue

            if not groups:
                # Defensive: active leases must either be dedicated or grouped.
                item = {**base, "status": "lease_conflict", "allocations": [], "lease_ids": [str(l["id"]) for l in active]}
                intervals.append(item)
                continue

            total_share = sum((dec(l.get("share", 0)) for l in active), Decimal("0"))
            if total_share > Decimal("100"):
                item = {
                    **base,
                    "status": "invalid_share_sum",
                    "allocations": [],
                    "lease_ids": sorted(str(l["id"]) for l in active),
                    "contracts": sorted({str(l["contract"]) for l in active}),
                    "share_sum_percent": total_share,
                }
                intervals.append(item)
                issues.append(_balance_issue("invalid_share_sum", item, blocking=bool(seg["effective_use"])))
                continue

            allocations = []
            for lease in sorted(active, key=lambda l: (str(l["contract"]), str(l["id"]))):
                share = dec(lease.get("share", 0))
                fraction = share / Decimal("100")
                allocations.append(
                    {
                        "contract": str(lease["contract"]),
                        "lease_id": str(lease["id"]),
                        "group": lease.get("group"),
                        "share_percent": share,
                        "fraction": fraction,
                        "allocated_seconds": dec(duration) * fraction,
                    }
                )
            residual_share = Decimal("100") - total_share
            item = {
                **base,
                "status": "allocated" if residual_share == 0 else "partially_unclaimed",
                "allocations": allocations,
                "lease_ids": sorted(str(l["id"]) for l in active),
                "contracts": sorted({a["contract"] for a in allocations}),
                "share_sum_percent": total_share,
                "unclaimed_fraction": residual_share / Decimal("100"),
                "unclaimed_seconds": dec(duration) * residual_share / Decimal("100"),
            }
            intervals.append(item)
            if residual_share > 0 and seg["effective_use"]:
                issues.append(_balance_issue("unclaimed_share", item, blocking=False))

    intervals.sort(key=lambda x: (x["start_ts"], x["meter"], x["fact_id"]))
    # De-duplicate structurally identical issues generated from adjacent pieces.
    unique: dict[str, dict[str, Any]] = {}
    for issue in issues:
        unique[issue["issue_id"]] = issue
    out_issues = sorted(unique.values(), key=lambda i: (i.get("start_ts") or -1, i["meter"], i["kind"]))
    return {"intervals": intervals, "issues": out_issues}


def _balance_issue(kind: str, item: dict[str, Any], blocking: bool, message: str | None = None) -> dict[str, Any]:
    return {
        "issue_id": issue_id(kind, item["meter"], item["start_ts"], item["end_ts"], ",".join(item.get("lease_ids", []))),
        "kind": kind,
        "blocking": blocking,
        "meter": item["meter"],
        "start_ts": item["start_ts"],
        "end_ts": item["end_ts"],
        "start": item["start"],
        "end": item["end"],
        "fact_id": item["fact_id"],
        "responsibility_id": item["responsibility_id"],
        "source_ids": item.get("source_ids", []),
        "lease_ids": item.get("lease_ids", []),
        "contracts": item.get("contracts", []),
        "unclaimed_seconds": item.get("unclaimed_seconds"),
        "message": message or kind,
    }
