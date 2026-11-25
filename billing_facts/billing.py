"""Exact rating, monthly free quotas, and invoice-line construction."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .timeutil import dec, iso, month_bounds, month_of, parse_ts, round_cents
from .timeline import issue_id

Row = dict[str, Any]


def _rate_intervals(rates: list[dict[str, Any]], contract: str):
    out = []
    for rate in rates:
        if str(rate["contract"]) != contract:
            continue
        start, end = parse_ts(rate["start"]), parse_ts(rate["end"])
        item = dict(rate)
        item["_start_ts"] = start
        item["_end_ts"] = end
        item["cents_per_sec_dec"] = dec(rate["cents_per_sec"])
        item["free_seconds_dec"] = dec(rate.get("free_seconds", 0))
        out.append(item)
    return sorted(out, key=lambda r: (r["_start_ts"], r["_end_ts"]))


def rate_overlap_issues(rates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_contract: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rate in rates:
        by_contract[str(rate["contract"])].append(rate)
    for contract, items in by_contract.items():
        intervals = sorted((parse_ts(r["start"]), parse_ts(r["end"]), r) for r in items)
        for (s1, e1, r1), (s2, e2, r2) in zip(intervals, intervals[1:]):
            if s2 < e1:
                issues.append(
                    {
                        "issue_id": issue_id("rate_overlap", contract, s1, e1, s2, e2),
                        "kind": "rate_overlap",
                        "blocking": True,
                        "contract": contract,
                        "start_ts": s2,
                        "end_ts": min(e1, e2),
                        "start": iso(s2),
                        "end": iso(min(e1, e2)),
                        "message": "rate service intervals overlap",
                        "rates": [r1, r2],
                    }
                )
    return issues


def rate_usage(
    responsibility: dict[str, Any],
    rates: list[dict[str, Any]],
    book_month_for_month: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create rated atoms and invoice lines for effective-use allocations."""
    atoms: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = list(rate_overlap_issues(rates))
    rate_cache: dict[str, list[dict[str, Any]]] = {}

    for resp in responsibility["intervals"]:
        if not resp.get("effective_use"):
            continue
        for alloc in resp.get("allocations", []):
            contract = alloc["contract"]
            contract_rates = rate_cache.setdefault(contract, _rate_intervals(rates, contract))
            s, e = int(resp["start_ts"]), int(resp["end_ts"])

            # Split at UTC natural-month boundaries.
            month_coords = {s, e}
            m = month_of(s)
            while True:
                ms, me = month_bounds(m)
                if s < me < e:
                    month_coords.add(me)
                if me >= e:
                    break
                m = month_of(me)
            coords = sorted(month_coords)

            for ms, me in zip(coords, coords[1:]):
                month = month_of(ms)
                rate_coords = {ms, me}
                for rate in contract_rates:
                    if rate["_start_ts"] < me and rate["_end_ts"] > ms:
                        rate_coords.add(max(ms, rate["_start_ts"]))
                        rate_coords.add(min(me, rate["_end_ts"]))
                rcoords = sorted(rate_coords)
                for rs, re_ in zip(rcoords, rcoords[1:]):
                    if rs >= re_:
                        continue
                    active_rates = [
                        r for r in contract_rates if r["_start_ts"] <= rs and r["_end_ts"] > rs
                    ]
                    duration = re_ - rs
                    common = {
                        "atom_id": f"atom:{resp['responsibility_id']}:{alloc['lease_id']}:{rs}-{re_}",
                        "fact_id": resp["fact_id"],
                        "responsibility_id": resp["responsibility_id"],
                        "meter": resp["meter"],
                        "boot": resp["boot"],
                        "source_id": resp.get("source_id"),
                        "source_ids": resp.get("source_ids", []),
                        "source_coverage": resp.get("source_coverage", []),
                        "lease_id": alloc["lease_id"],
                        "group": alloc.get("group"),
                        "contract": contract,
                        "month": month,
                        "book_month": (book_month_for_month or {}).get(month, month),
                        "start_ts": rs,
                        "end_ts": re_,
                        "start": iso(rs),
                        "end": iso(re_),
                        "device_seconds": duration,
                        "fraction": alloc["fraction"],
                        "allocated_seconds": dec(duration) * alloc["fraction"],
                        "share_percent": alloc["share_percent"],
                    }
                    if not active_rates:
                        issues.append(
                            {
                                "issue_id": issue_id("missing_rate", contract, rs, re_, resp["meter"]),
                                "kind": "missing_rate",
                                "blocking": True,
                                "contract": contract,
                                "meter": resp["meter"],
                                "start_ts": rs,
                                "end_ts": re_,
                                "start": iso(rs),
                                "end": iso(re_),
                                "fact_id": resp["fact_id"],
                                "source_ids": resp.get("source_ids", []),
                                "lease_id": alloc["lease_id"],
                                "message": "no rate covers this contract service interval",
                            }
                        )
                        atoms.append({**common, "rate": None, "free_seconds": Decimal("0"), "free_used_seconds": Decimal("0"), "billable_seconds": common["allocated_seconds"]})
                        continue
                    if len(active_rates) > 1:
                        issues.append(
                            {
                                "issue_id": issue_id("rate_overlap_atom", contract, rs, re_),
                                "kind": "rate_overlap",
                                "blocking": True,
                                "contract": contract,
                                "meter": resp["meter"],
                                "start_ts": rs,
                                "end_ts": re_,
                                "start": iso(rs),
                                "end": iso(re_),
                                "message": "multiple rates cover same interval",
                            }
                        )
                    rate = active_rates[0]
                    atoms.append(
                        {
                            **common,
                            "rate_start": iso(rate["_start_ts"]),
                            "rate_end": iso(rate["_end_ts"]),
                            "rate_cents_per_sec": rate["cents_per_sec_dec"],
                            "free_quota_seconds": rate["free_seconds_dec"],
                            "free_used_seconds": Decimal("0"),
                            "billable_seconds": common["allocated_seconds"],
                        }
                    )

    _consume_free_quotas(atoms)
    lines = _build_lines(atoms)
    unique_issues: dict[str, dict[str, Any]] = {}
    for issue in issues:
        unique_issues[issue["issue_id"]] = issue
    return {"atoms": atoms, "lines": lines, "issues": list(unique_issues.values())}


def _consume_free_quotas(atoms: list[dict[str, Any]]) -> None:
    """Consume each monthly quota in (time, meter, source) order.

    Simultaneous devices are ordered on every second, matching the dictionary's
    lexicographic occurrence key without expanding long intervals second by
    second.
    """
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for atom in atoms:
        if not atom.get("rate_start"):
            continue
        key = (atom["contract"], atom["month"], atom["rate_start"], atom["rate_end"])
        groups[key].append(atom)

    for (contract, month, rate_start, rate_end), group in groups.items():
        remaining = dec(group[0]["free_quota_seconds"])
        coords = sorted({x for atom in group for x in (atom["start_ts"], atom["end_ts"])})
        for s, e in zip(coords, coords[1:]):
            if remaining <= 0:
                break
            active = [a for a in group if a["start_ts"] <= s and a["end_ts"] >= e]
            if not active:
                continue
            active.sort(key=lambda a: (a["start_ts"], str(a["meter"]), str(a["source_id"] or ""), str(a["lease_id"]), a["atom_id"]))
            length = e - s
            total_per_second = sum((a["fraction"] for a in active), Decimal("0"))
            slice_total = total_per_second * length
            if remaining >= slice_total:
                for atom in active:
                    used = dec(length) * atom["fraction"]
                    atom["free_used_seconds"] += used
                    atom["billable_seconds"] -= used
                remaining -= slice_total
                continue

            full_seconds = int(remaining // total_per_second) if total_per_second else 0
            full_seconds = max(0, min(full_seconds, length))
            remainder = remaining - full_seconds * total_per_second
            for atom in active:
                used = dec(full_seconds) * atom["fraction"]
                if remainder > 0:
                    partial = min(atom["fraction"], remainder)
                    used += partial
                    remainder -= partial
                atom["free_used_seconds"] += used
                atom["billable_seconds"] -= used
            remaining = Decimal("0")


def _build_lines(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for atom in atoms:
        if not atom.get("rate_start"):
            continue
        key = (
            "usage",
            atom["book_month"],
            atom["contract"],
            atom["month"],
            atom["rate_start"],
            atom["rate_end"],
            str(atom["rate_cents_per_sec"]),
        )
        groups[key].append(atom)

    lines: list[dict[str, Any]] = []
    for key, group in sorted(groups.items(), key=lambda kv: kv[0][1:]):
        kind, book_month, contract, for_month, rate_start, rate_end, rate_text = key
        exact_seconds = sum((a["allocated_seconds"] for a in group), Decimal("0"))
        free_seconds = sum((a["free_used_seconds"] for a in group), Decimal("0"))
        billable_seconds = sum((a["billable_seconds"] for a in group), Decimal("0"))
        exact_amount = sum((a["billable_seconds"] * a["rate_cents_per_sec"] for a in group), Decimal("0"))
        line_id = f"LINE-{for_month}-{contract}-{rate_start.replace(':', '').replace('-', '')}-{rate_end.replace(':', '').replace('-', '')}"
        coverage = [
            {
                "atom_id": a["atom_id"],
                "fact_id": a["fact_id"],
                "meter": a["meter"],
                "source_id": a["source_id"],
                "source_ids": a["source_ids"],
                "source_coverage": a.get("source_coverage", []),
                "lease_id": a["lease_id"],
                "start": a["start"],
                "end": a["end"],
                "device_seconds": a["device_seconds"],
                "share_percent": a["share_percent"],
                "allocated_seconds": a["allocated_seconds"],
                "free_seconds": a["free_used_seconds"],
                "billable_seconds": a["billable_seconds"],
            }
            for a in sorted(group, key=lambda a: (a["start_ts"], a["meter"], str(a["source_id"] or "")))
        ]
        lines.append(
            {
                "line_id": line_id,
                "kind": "usage",
                "contract": contract,
                "for_month": for_month,
                "book_month": book_month,
                "rate": {
                    "start": rate_start,
                    "end": rate_end,
                    "cents_per_sec": rate_text,
                },
                "exact_allocated_seconds": exact_seconds,
                "free_seconds": free_seconds,
                "billable_seconds": billable_seconds,
                "exact_amount_cents": exact_amount,
                "invoice_amount_cents": round_cents(exact_amount),
                "coverage": coverage,
                "basis": [
                    "half-open [start,end); overlapping logs deduplicated by state priority",
                    "lease shares retained as exact Decimal fractions",
                    "free quota ordered by occurrence second, meter, source id",
                    "line total rounded once with ROUND_HALF_UP to whole cents",
                ],
            }
        )
    return lines
