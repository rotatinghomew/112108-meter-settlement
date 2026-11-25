"""Revision-aware source selection."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable

Row = dict[str, Any]
Body = dict[str, Any]


def canonical(body: Body) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rev(body: Body) -> int:
    value = body.get("rev", 1)
    return int(value)


def select_versioned(
    rows: list[Row],
    kind: str,
    key: Callable[[Body], str],
    before_seq: int | None = None,
) -> tuple[dict[str, Body], dict[str, list[Body]]]:
    """Return current body per identity and same-revision conflicts.

    Larger ``rev`` replaces the whole record.  Identical repetitions are
    idempotent.  Different payloads at the same revision are disputed and are
    not silently chosen.
    """
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row["kind"] != kind:
            continue
        if before_seq is not None and row["seq"] >= before_seq:
            continue
        groups[key(row["body"])].append(row)

    effective: dict[str, Body] = {}
    disputes: dict[str, list[Body]] = {}
    for identity, group in groups.items():
        max_rev = max(_rev(r["body"]) for r in group)
        latest = [r["body"] for r in group if _rev(r["body"]) == max_rev]
        distinct: dict[str, Body] = {}
        for body in latest:
            distinct.setdefault(canonical(body), body)
        if len(distinct) == 1:
            effective[identity] = next(iter(distinct.values()))
        else:
            # Do not choose between same-revision contradictory payloads.
            disputes[identity] = list(distinct.values())
    return effective, disputes


def effective_logs(rows: list[Row], before_seq: int | None = None):
    return select_versioned(rows, "log", lambda b: str(b["id"]), before_seq)


def effective_leases(rows: list[Row], before_seq: int | None = None):
    return select_versioned(rows, "lease", lambda b: str(b["id"]), before_seq)


def effective_clocks(rows: list[Row], before_seq: int | None = None):
    return select_versioned(
        rows,
        "clock",
        lambda b: f'{b["meter"]}\u0000{b["boot"]}',
        before_seq,
    )


def all_rates(rows: list[Row], before_seq: int | None = None) -> list[Body]:
    out: list[Body] = []
    seen: set[str] = set()
    for row in rows:
        if row["kind"] != "rate":
            continue
        if before_seq is not None and row["seq"] >= before_seq:
            continue
        text = canonical(row["body"])
        if text not in seen:
            seen.add(text)
            out.append(row["body"])
    return out
