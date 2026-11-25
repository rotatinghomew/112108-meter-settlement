"""Optional maintenance/calibration evidence.

The exchange dictionary in the initial batch defines ``fault`` and ``cal`` log
states but no separate confirmation record.  Later batches may add repair or
maintenance confirmations.  This module deliberately accepts several plain
spellings and only converts a non-use state into billable effective use when
the evidence explicitly says the waiting/calibration time is chargeable.
"""
from __future__ import annotations

from typing import Any

from .timeutil import parse_ts

Row = dict[str, Any]

EVIDENCE_KINDS = {
    "repair",
    "repair_confirmation",
    "maintenance",
    "maintenance_confirmation",
    "confirmation",
    "service_confirmation",
}

TRUE_TOKENS = ("billable", "chargeable", "tenant", "user_fault", "operator", "improper", "人为", "承租方")
FALSE_TOKENS = ("equipment", "device_fault", "vendor", "warranty", "not_billable", "free", "设备", "维修方")


def _explicit_bool(body: dict[str, Any]) -> bool | None:
    for key in ("billable_as_use", "effective_use", "count_as_use", "billable", "chargeable", "valid_use"):
        if key in body:
            return bool(body[key])
    text = " ".join(str(body.get(k, "")) for k in ("determination", "result", "cause", "reason", "status", "note")).lower()
    if any(token in text for token in TRUE_TOKENS):
        return True
    if any(token in text for token in FALSE_TOKENS):
        return False
    return None


def apply_evidence(timeline: dict[str, Any], rows: list[Row], before_seq: int | None = None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in rows:
        if row["kind"] not in EVIDENCE_KINDS:
            continue
        if before_seq is not None and row["seq"] >= before_seq:
            continue
        body = row["body"]
        if "start" not in body or "end" not in body:
            continue
        meter = body.get("meter")
        start, end = parse_ts(body["start"]), parse_ts(body["end"])
        determination = _explicit_bool(body)
        applies = set(body.get("applies_to_states", body.get("states", [])) or [])
        if not applies:
            applies = {"fault"} if row["kind"].startswith("repair") or "repair" in row["kind"] else {"fault", "cal"}
        ref = {
            "kind": row["kind"],
            "id": body.get("id"),
            "start": body["start"],
            "end": body["end"],
            "determination": determination,
        }
        for seg in timeline["segments"]:
            if meter is not None and seg["meter"] != meter:
                continue
            if seg["state"] not in applies:
                continue
            if max(start, seg["start_ts"]) >= min(end, seg["end_ts"]):
                continue
            seg.setdefault("evidence", []).append(ref)
            if determination is True:
                seg["effective_use"] = True
                seg["usage_class"] = "evidence_confirmed_chargeable_" + seg["state"]
            elif determination is False:
                seg["effective_use"] = False
                seg["usage_class"] = "evidence_confirmed_non_chargeable_" + seg["state"]
    return issues
