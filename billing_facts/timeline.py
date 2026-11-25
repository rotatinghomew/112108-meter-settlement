"""Unified-time state timeline construction."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from .sources import effective_clocks, effective_logs
from .timeutil import iso, parse_ts

Row = dict[str, Any]

# Higher number wins on overlap.  The first three implement the dictionary's
# explicit fault > calibration > use precedence.
STATE_PRIORITY = {
    "fault": 5,
    "cal": 4,
    "use": 3,
    "run": 2,
    "running": 2,
    "idle": 1,
    "stop": 1,
    "stopped": 1,
    "shutdown": 1,
    "off": 1,
    "unknown": 0,
}

NON_USE_STATES = {"cal", "fault", "idle", "stop", "stopped", "shutdown", "off"}


def issue_id(kind: str, *parts: Any) -> str:
    raw = "|".join([kind, *map(str, parts)])
    return "ISS-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _clock_mapping(clock: dict[str, Any] | None):
    if not clock:
        return None
    zero = parse_ts(clock["zero"])
    from_tick = int(clock.get("from_tick", 0))
    to_tick = int(clock.get("to_tick", 0))

    def utc(tick: int) -> int:
        return zero + int(tick)

    return {"zero": zero, "from_tick": from_tick, "to_tick": to_tick, "utc": utc, "rev": clock.get("rev", 1)}


def _state_rank(state: str) -> int:
    return STATE_PRIORITY.get(str(state), 0)


def build_timeline(rows: list[Row], before_seq: int | None = None) -> dict[str, Any]:
    logs, log_disputes = effective_logs(rows, before_seq)
    clocks, clock_disputes = effective_clocks(rows, before_seq)

    by_boot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for body in logs.values():
        by_boot[(body["meter"], body["boot"])].append(body)

    # Ensure meters/boots appearing only in disputed ids are represented.
    for bodies in log_disputes.values():
        for body in bodies:
            by_boot.setdefault((body["meter"], body["boot"]), [])

    segments: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for (meter, boot), boot_logs in sorted(by_boot.items()):
        ckey = f"{meter}\u0000{boot}"
        clock_body = clocks.get(ckey)
        clock = _clock_mapping(clock_body)

        if ckey in clock_disputes:
            for bodies in clock_disputes[ckey]:
                issues.append(
                    {
                        "issue_id": issue_id("clock_revision_conflict", meter, boot),
                        "kind": "clock_revision_conflict",
                        "blocking": True,
                        "meter": meter,
                        "boot": boot,
                        "source_ids": sorted({str(b.get("id", "clock")) for b in bodies if "id" in b}),
                        "message": "same clock revision has contradictory payloads",
                        "bodies": bodies,
                    }
                )

        if clock is None and ckey not in clock_disputes:
            for log in sorted(boot_logs, key=lambda b: str(b["id"])):
                issues.append(
                    {
                        "issue_id": issue_id("missing_clock", log["id"]),
                        "kind": "missing_clock",
                        "blocking": True,
                        "meter": meter,
                        "boot": boot,
                        "tick_start": int(log["start"]),
                        "tick_end": int(log["end"]),
                        "source_ids": [log["id"]],
                        "message": "no effective clock correspondence",
                    }
                )

        # Same-revision log contradictions preserve the disputed range.
        for identity, bodies in sorted(log_disputes.items()):
            relevant = [b for b in bodies if b.get("meter") == meter and b.get("boot") == boot]
            if not relevant:
                continue
            lo = min(int(b["start"]) for b in relevant)
            hi = max(int(b["end"]) for b in relevant)
            start_ts = clock["utc"](lo) if clock else None
            end_ts = clock["utc"](hi) if clock else None
            issues.append(
                {
                    "issue_id": issue_id("log_revision_conflict", identity, lo, hi),
                    "kind": "log_revision_conflict",
                    "blocking": True,
                    "meter": meter,
                    "boot": boot,
                    "tick_start": lo,
                    "tick_end": hi,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "start": iso(start_ts),
                    "end": iso(end_ts),
                    "source_ids": [identity],
                    "message": "same log revision has contradictory payloads",
                }
            )

        if not clock:
            continue

        covered_logs: list[dict[str, Any]] = []
        for log in sorted(boot_logs, key=lambda b: (int(b["start"]), str(b["id"]))):
            ls, le = int(log["start"]), int(log["end"])
            cs, ce = clock["from_tick"], clock["to_tick"]
            overlap_s, overlap_e = max(ls, cs), min(le, ce)

            # Portions outside the valid correspondence remain suspended.
            uncovered: list[tuple[int, int]] = []
            if ls < overlap_s:
                uncovered.append((ls, min(le, overlap_s)))
            if overlap_e < le:
                uncovered.append((max(ls, overlap_e), le))
            for us, ue in uncovered:
                if us >= ue:
                    continue
                uts, ute = clock["utc"](us), clock["utc"](ue)
                issues.append(
                    {
                        "issue_id": issue_id("clock_coverage", log["id"], us, ue),
                        "kind": "clock_coverage_gap",
                        "blocking": True,
                        "meter": meter,
                        "boot": boot,
                        "tick_start": us,
                        "tick_end": ue,
                        "start_ts": uts,
                        "end_ts": ute,
                        "start": iso(uts),
                        "end": iso(ute),
                        "source_ids": [log["id"]],
                        "state": log.get("state"),
                        "message": "log tick interval is outside the valid clock coverage",
                    }
                )

            if overlap_s < overlap_e:
                item = dict(log)
                item["_covered_start"] = overlap_s
                item["_covered_end"] = overlap_e
                covered_logs.append(item)

        if not covered_logs:
            continue

        coords = sorted(
            {x for log in covered_logs for x in (int(log["_covered_start"]), int(log["_covered_end"]))}
        )
        for a, b in zip(coords, coords[1:]):
            if a >= b:
                continue
            active = [
                log
                for log in covered_logs
                if int(log["_covered_start"]) <= a and int(log["_covered_end"]) >= b
            ]
            if not active:
                state = "no_evidence"
                winner_id = None
                source_ids: list[str] = []
            else:
                winner = min(
                    active,
                    key=lambda log: (
                        -_state_rank(str(log.get("state", "unknown"))),
                        str(log["id"]),
                    ),
                )
                state = str(winner.get("state", "unknown"))
                winner_id = str(winner["id"])
                source_ids = sorted({str(log["id"]) for log in active})

            start_ts, end_ts = clock["utc"](a), clock["utc"](b)
            fact_id = f"fact:{meter}:{boot}:{a}-{b}"
            segment = {
                "fact_id": fact_id,
                "meter": meter,
                "boot": boot,
                "tick_start": a,
                "tick_end": b,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "start": iso(start_ts),
                "end": iso(end_ts),
                "seconds": b - a,
                "state": state,
                "effective_use": state == "use",
                "source_id": winner_id,
                "source_ids": source_ids,
                "source_coverage": [
                    {"source_id": sid, "start": iso(clock["utc"](a)), "end": iso(clock["utc"](b))}
                    for sid in source_ids
                ],
                "clock_rev": clock["rev"],
            }
            segments.append(segment)

    segments = _coalesce_segments(segments)
    for seg in segments:
        if seg["state"] == "unknown":
            issues.append(
                {
                    "issue_id": issue_id("unknown_state", seg["meter"], seg["boot"], seg["tick_start"], seg["tick_end"]),
                    "kind": "unknown_state",
                    "blocking": True,
                    "meter": seg["meter"],
                    "boot": seg["boot"],
                    "tick_start": seg["tick_start"],
                    "tick_end": seg["tick_end"],
                    "start_ts": seg["start_ts"],
                    "end_ts": seg["end_ts"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "source_ids": seg["source_ids"],
                    "fact_id": seg["fact_id"],
                    "message": "log state is unknown and no higher-priority known state overlaps it",
                }
            )
        elif seg["state"] == "no_evidence":
            issues.append(
                {
                    "issue_id": issue_id("log_gap", seg["meter"], seg["boot"], seg["tick_start"], seg["tick_end"]),
                    "kind": "log_gap",
                    "blocking": True,
                    "meter": seg["meter"],
                    "boot": seg["boot"],
                    "tick_start": seg["tick_start"],
                    "tick_end": seg["tick_end"],
                    "start_ts": seg["start_ts"],
                    "end_ts": seg["end_ts"],
                    "start": seg["start"],
                    "end": seg["end"],
                    "source_ids": seg["source_ids"],
                    "fact_id": seg["fact_id"],
                    "message": "continuous boot interval has no log state evidence",
                }
            )

    segments.sort(key=lambda s: (s["start_ts"], s["meter"], s["boot"], s["tick_start"]))
    issues.sort(key=lambda i: (i.get("start_ts") or -1, i["meter"], i["kind"], str(i.get("issue_id"))))
    return {"segments": segments, "issues": issues}


def _coalesce_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent pieces with the same winning state/source."""
    by_boot: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for seg in segments:
        by_boot[(seg["meter"], seg["boot"])].append(seg)

    result: list[dict[str, Any]] = []
    for boot_segments in by_boot.values():
        boot_segments.sort(key=lambda s: s["tick_start"])
        current: dict[str, Any] | None = None
        for seg in boot_segments:
            if (
                current is not None
                and current["tick_end"] == seg["tick_start"]
                and current["state"] == seg["state"]
                and current["source_id"] == seg["source_id"]
                and current["clock_rev"] == seg["clock_rev"]
            ):
                current["tick_end"] = seg["tick_end"]
                current["end_ts"] = seg["end_ts"]
                current["end"] = seg["end"]
                current["seconds"] += seg["seconds"]
                current["source_ids"] = sorted(set(current["source_ids"]) | set(seg["source_ids"]))
                current.setdefault("source_coverage", []).extend(seg.get("source_coverage", []))
                current["source_coverage"] = _compact_source_coverage(current["source_coverage"])
            else:
                if current is not None:
                    current["fact_id"] = (
                        f"fact:{current['meter']}:{current['boot']}:"
                        f"{current['tick_start']}-{current['tick_end']}"
                    )
                    result.append(current)
                current = dict(seg)
        if current is not None:
            current["fact_id"] = (
                f"fact:{current['meter']}:{current['boot']}:"
                f"{current['tick_start']}-{current['tick_end']}"
            )
            result.append(current)
    return result


def _compact_source_coverage(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in coverage:
        by_source[str(item["source_id"])].append(item)
    result = []
    for source_id, items in sorted(by_source.items()):
        items.sort(key=lambda x: x["start"])
        start = items[0]["start"]
        end = items[0]["end"]
        for item in items[1:]:
            if item["start"] == end:
                end = item["end"]
            else:
                result.append({"source_id": source_id, "start": start, "end": end})
                start, end = item["start"], item["end"]
        result.append({"source_id": source_id, "start": start, "end": end})
    return sorted(result, key=lambda x: (x["source_id"], x["start"]))

