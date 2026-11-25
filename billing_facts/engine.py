"""Incremental fact warehouse, monthly close, and closed-month reconciliation."""
from __future__ import annotations

import copy
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .billing import rate_usage
from .evidence import apply_evidence
from .responsibility import assign_responsibility
from .sources import canonical, effective_logs
from .timeutil import dec, iso, month_intersect, next_month, parse_ts, round_cents
from .timeline import build_timeline


class CloseRejected(ValueError):
    pass


class Warehouse:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.posted: dict[str, dict[str, Any]] = {}
        self.posted_conflicts: dict[str, list[dict[str, Any]]] = {}
        self.closed: dict[str, dict[str, Any]] = {}
        self.close_attempts: list[dict[str, Any]] = []
        self.journal: dict[str, dict[str, Any]] = {}
        self.recon_state: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ input
    def ingest_record(self, row: dict[str, Any]) -> dict[str, Any]:
        seq = len(self.rows) + 1
        stored = {"seq": seq, "step": row.get("step"), "kind": row["kind"], "body": row["body"]}
        self.rows.append(stored)
        result: dict[str, Any] = {"seq": seq, "kind": row["kind"]}

        if row["kind"] == "posted":
            self._ingest_posted(stored)
        elif row["kind"] == "close":
            try:
                snapshot = self.close_month(row["body"], seq=seq)
                result["closed"] = snapshot["month"]
                result["close_id"] = snapshot["close_id"]
            except CloseRejected as exc:
                result["rejected"] = str(exc)
        return result

    def ingest_jsonl(self, path: str) -> list[dict[str, Any]]:
        import json
        from pathlib import Path

        results = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                results.append(self.ingest_record(json.loads(line)))
        return results

    def _ingest_posted(self, row: dict[str, Any]) -> None:
        body = row["body"]
        rid = str(body["id"])
        if rid in self.posted:
            if canonical(self.posted[rid]["body"]) != canonical(body):
                self.posted_conflicts.setdefault(rid, [self.posted[rid]["body"], body])
            return
        self.posted[rid] = row

    # ------------------------------------------------------------------- views
    def view(self, before_seq: int | None = None) -> dict[str, Any]:
        timeline = build_timeline(self.rows, before_seq)
        evidence_issues = apply_evidence(timeline, self.rows, before_seq)
        responsibility = assign_responsibility(timeline, self.rows, before_seq)
        rates = []
        seen = set()
        for row in self.rows:
            if row["kind"] != "rate":
                continue
            if before_seq is not None and row["seq"] >= before_seq:
                continue
            text = canonical(row["body"])
            if text not in seen:
                seen.add(text)
                rates.append(row["body"])
        rated = rate_usage(responsibility, rates)
        issues = timeline["issues"] + evidence_issues + responsibility["issues"] + rated["issues"]
        # De-duplicate by issue_id while preserving order.
        dedup: dict[str, dict[str, Any]] = {}
        for issue in issues:
            dedup.setdefault(issue["issue_id"], issue)
        return {
            "timeline": timeline["segments"],
            "responsibility": responsibility["intervals"],
            "atoms": rated["atoms"],
            "lines": rated["lines"],
            "issues": list(dedup.values()),
        }

    # ------------------------------------------------------------------- close
    def close_month(self, close_body: dict[str, Any], seq: int | None = None) -> dict[str, Any]:
        month = str(close_body["month"])
        close_id = str(close_body["id"])
        if month in self.closed:
            attempt = {"close_id": close_id, "month": month, "seq": seq, "status": "ignored_month_already_closed"}
            self.close_attempts.append(attempt)
            return self.closed[month]

        as_of = seq if seq is not None else len(self.rows) + 1
        view = self.view(before_seq=as_of)
        holds = self._normalize_holds(close_body.get("allowed_holds", []), as_of)
        uncovered = self._uncovered_blocking_issues(view["issues"], month, holds, as_of)
        if uncovered:
            summary = [
                {
                    "issue_id": i.get("issue_id"),
                    "kind": i.get("kind"),
                    "meter": i.get("meter"),
                    "start": i.get("start"),
                    "end": i.get("end"),
                    "source_ids": i.get("source_ids", []),
                }
                for i in uncovered
            ]
            attempt = {
                "close_id": close_id,
                "month": month,
                "seq": seq,
                "status": "rejected",
                "uncovered_issues": summary,
            }
            self.close_attempts.append(attempt)
            raise CloseRejected(f"close {close_id} rejected: {len(uncovered)} unresolved interval(s) not covered by allowed_holds")

        # Reconcile older closed months into this target book month before
        # snapshotting the new close.
        journal_entries: list[dict[str, Any]] = []
        for old_month in sorted(self.closed):
            if old_month < month:
                journal_entries.extend(
                    self._reconcile_contract(old_month, month, close_id, as_of, view)
                )

        month_lines = [line for line in view["lines"] if line["for_month"] == month]
        totals_exact: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        totals_cents: dict[str, int] = defaultdict(int)
        for line in month_lines:
            totals_exact[line["contract"]] += line["exact_amount_cents"]
            totals_cents[line["contract"]] += line["invoice_amount_cents"]

        month_issues = []
        open_unclaimed = []
        for issue in view["issues"]:
            part = self._issue_month_part(issue, month)
            if part is not None or (issue.get("start_ts") is None and issue.get("blocking")):
                month_issues.append(issue.get("issue_id"))
            if issue.get("kind") in {"unclaimed", "unclaimed_share"} and part is not None:
                public = self._public_issue(issue)
                ps, pe = part
                public["month_part_start"] = iso(ps)
                public["month_part_end"] = iso(pe)
                open_unclaimed.append(public)

        carried_old_issues = self._carried_uncovered_old_month_issues(view)
        adjustment_cents: dict[str, int] = defaultdict(int)
        for entry in journal_entries:
            adjustment_cents[entry["contract"]] += int(entry["amount_cents"])
        close_period_total = dict(totals_cents)
        for contract, amount in adjustment_cents.items():
            close_period_total[contract] = close_period_total.get(contract, 0) + amount

        snapshot = {
            "close_id": close_id,
            "month": month,
            "seq": seq,
            "status": "closed",
            "allowed_holds": [self._hold_json(h) for h in holds],
            "usage_lines": month_lines,
            "journal_entry_ids": [e["entry_id"] for e in journal_entries],
            "journal_entries": [copy.deepcopy(e) for e in journal_entries],
            "receivable_exact_by_contract": dict(totals_exact),
            "receivable_by_contract_cents": dict(totals_cents),
            "prior_month_adjustment_net_by_contract_cents": dict(adjustment_cents),
            "close_period_invoice_total_by_contract_cents": close_period_total,
            "issue_ids_in_month": month_issues,
            "open_unclaimed": open_unclaimed,
            "carried_unresolved_old_month_issues": carried_old_issues,
            "original_basis": "Rates/free quota and effective facts as of this close; never replaced by a rerun.",
        }
        self.closed[month] = snapshot
        self.close_attempts.append({"close_id": close_id, "month": month, "seq": seq, "status": "closed"})
        return snapshot

    def _normalize_holds(self, raw_holds: list[Any], as_of: int) -> list[dict[str, Any]]:
        logs, _ = effective_logs(self.rows, as_of)
        holds = []
        for raw in raw_holds:
            if isinstance(raw, dict):
                ref = raw.get("ref") or raw.get("id") or raw.get("meter")
                start_s = raw["start"]
                end_s = raw["end"]
            else:
                ref, start_s, end_s = raw
            start, end = self._parse_hold_ts(start_s), self._parse_hold_ts(end_s)
            meter = str(ref)
            if str(ref) in logs:
                meter = str(logs[str(ref)]["meter"])
            holds.append({"ref": str(ref), "meter": meter, "start_ts": start, "end_ts": end})
        return holds

    @staticmethod
    def _parse_hold_ts(value: Any) -> int:
        # timeutil.parse_ts is imported lazily is unnecessary; direct wrapper.
        from .timeutil import parse_ts

        return parse_ts(value)

    @staticmethod
    def _hold_json(hold: dict[str, Any]) -> dict[str, Any]:
        return {"ref": hold["ref"], "meter": hold["meter"], "start": iso(hold["start_ts"]), "end": iso(hold["end_ts"])}

    def _issue_month_part(self, issue: dict[str, Any], month: str) -> tuple[int, int] | None:
        if issue.get("start_ts") is None or issue.get("end_ts") is None:
            return None
        return month_intersect(int(issue["start_ts"]), int(issue["end_ts"]), month)

    def _uncovered_blocking_issues(
        self, issues: list[dict[str, Any]], month: str, holds: list[dict[str, Any]], as_of: int
    ) -> list[dict[str, Any]]:
        uncovered: list[dict[str, Any]] = []
        for issue in issues:
            if not issue.get("blocking"):
                continue
            part = self._issue_month_part(issue, month)
            if part is None:
                if issue.get("start_ts") is None:
                    source_ids = set(issue.get("source_ids", []))
                    if any(h["ref"] in source_ids or h["meter"] == issue.get("meter") for h in holds):
                        continue
                    uncovered.append(issue)
                continue
            ps, pe = part
            meter = issue.get("meter")
            source_ids = set(issue.get("source_ids", []))
            covered = any(
                (h["meter"] == meter or h["ref"] in source_ids)
                and h["start_ts"] <= ps
                and h["end_ts"] >= pe
                for h in holds
            )
            if not covered:
                uncovered.append(issue)
        return uncovered

    def _carried_uncovered_old_month_issues(self, view: dict[str, Any]) -> list[dict[str, Any]]:
        carried: list[dict[str, Any]] = []
        for old_month, snap in sorted(self.closed.items()):
            old_holds = [
                {**h, "start_ts": parse_ts(h["start"]), "end_ts": parse_ts(h["end"])}
                for h in snap.get("allowed_holds", [])
            ]
            for issue in view["issues"]:
                if not issue.get("blocking"):
                    continue
                part = self._issue_month_part(issue, old_month)
                if part is None:
                    continue
                ps, pe = part
                source_ids = set(issue.get("source_ids", []))
                covered = any(
                    (h["meter"] == issue.get("meter") or h["ref"] in source_ids)
                    and h["start_ts"] <= ps
                    and h["end_ts"] >= pe
                    for h in old_holds
                )
                if not covered:
                    item = self._public_issue(issue)
                    item["source_closed_month"] = old_month
                    item["source_close_id"] = snap["close_id"]
                    item["month_part_start"] = iso(ps)
                    item["month_part_end"] = iso(pe)
                    carried.append(item)
        return carried

    # ------------------------------------------------------------- adjustments
    def _posted_for(self, month: str, contract: str, before_seq: int | None = None) -> list[dict[str, Any]]:
        out = []
        # self.posted de-duplicates repeated delivery of the same receipt id.
        for row in self.posted.values():
            if before_seq is not None and row["seq"] >= before_seq:
                continue
            body = row["body"]
            if str(body.get("for_month")) == month and str(body.get("contract")) == contract:
                out.append(row)
        return sorted(out, key=lambda r: (r["seq"], str(r["body"]["id"])))

    def _reconcile_contract(
        self, old_month: str, target_book_month: str, close_id: str, as_of: int, view: dict[str, Any]
    ) -> list[dict[str, Any]]:
        snap = self.closed[old_month]
        current_lines = [
            line for line in view["lines"] if line["for_month"] == old_month and line["kind"] == "usage"
        ]
        current_cents: dict[str, int] = defaultdict(int)
        current_exact: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        current_line_ids: dict[str, list[str]] = defaultdict(list)
        current_line_objects: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for line in current_lines:
            c = line["contract"]
            current_cents[c] += line["invoice_amount_cents"]
            current_exact[c] += line["exact_amount_cents"]
            current_line_ids[c].append(line["line_id"])
            current_line_objects[c].append(line)

        contracts = set(snap["receivable_by_contract_cents"]) | set(current_cents)
        entries: list[dict[str, Any]] = []
        for contract in sorted(contracts):
            key = f"{old_month}|{contract}"
            state = self.recon_state.get(key)
            original_cents = int(snap["receivable_by_contract_cents"].get(contract, 0))
            original_exact = dec(snap.get("receivable_exact_by_contract", {}).get(contract, original_cents))
            corrected_cents = int(current_cents.get(contract, 0))
            corrected_exact = current_exact.get(contract, Decimal("0"))
            posted_rows = self._posted_for(old_month, contract, as_of)
            posted_cents = sum(int(r["body"]["amount_cents"]) for r in posted_rows)

            if state is None:
                net_amount = corrected_cents - posted_cents
                net_exact = corrected_exact - Decimal(posted_cents)
                booking_gap = original_cents - posted_cents
                fact_delta = corrected_cents - original_cents
                if net_amount != 0:
                    entry = self._make_journal_entry(
                        kind="closed_month_reconciliation",
                        old_month=old_month,
                        contract=contract,
                        target_book_month=target_book_month,
                        close_id=close_id,
                        as_of=as_of,
                        amount_cents=net_amount,
                        amount_exact_cents=net_exact,
                        reason=(
                            "Compare the corrected complete receivable for the original month "
                            "with amounts actually posted; post this signed supplement/reversal "
                            "in the next open book month."
                        ),
                        basis={
                            "original_close_id": snap["close_id"],
                            "original_receivable_cents": original_cents,
                            "original_exact_receivable_cents": original_exact,
                            "corrected_receivable_cents": corrected_cents,
                            "corrected_exact_receivable_cents": corrected_exact,
                            "posted_cents_at_calculation": posted_cents,
                            "net_amount_cents": net_amount,
                            "net_exact_before_line_rounding_cents": net_exact,
                            "memo_booking_gap_original_minus_posted_cents": booking_gap,
                            "memo_fact_correction_corrected_minus_original_cents": fact_delta,
                            "memo_components": [
                                {
                                    "component": "original_account_vs_posted",
                                    "amount_cents": booking_gap,
                                    "meaning": "Would make posted ledger equal the original close account.",
                                },
                                {
                                    "component": "closed_month_fact_correction",
                                    "amount_cents": fact_delta,
                                    "meaning": "Old-rate correction from original account to current old-month recomputation.",
                                },
                            ],
                            "old_rates_only": True,
                            "posted_receipts": [self._receipt_ref(r) for r in posted_rows],
                            "original_line_ids": [l["line_id"] for l in snap["usage_lines"] if l["contract"] == contract],
                            "corrected_line_ids": current_line_ids.get(contract, []),
                            "corrected_lines_at_calculation": current_line_objects.get(contract, []),
                        },
                    )
                    entries.append(entry)
                self.recon_state[key] = {
                    "old_month": old_month,
                    "contract": contract,
                    "last_corrected_receivable_cents": corrected_cents,
                    "last_corrected_exact_cents": corrected_exact,
                    "initial_original_receivable_cents": original_cents,
                    "initial_posted_cents": posted_cents,
                    "initial_entry_amount_cents": net_amount,
                    "close_id": close_id,
                }
            else:
                last_cents = int(state["last_corrected_receivable_cents"])
                last_exact = dec(state.get("last_corrected_exact_cents", last_cents))
                delta_exact = corrected_exact - last_exact
                delta = round_cents(delta_exact)
                if delta != 0 or delta_exact != 0:
                    entry = self._make_journal_entry(
                        kind="incremental_fact_correction",
                        old_month=old_month,
                        contract=contract,
                        target_book_month=target_book_month,
                        close_id=close_id,
                        as_of=as_of,
                        amount_cents=delta,
                        amount_exact_cents=delta_exact,
                        reason="Later correction after an earlier adjustment period was closed.",
                        basis={
                            "previous_corrected_receivable_cents": last_cents,
                            "new_corrected_receivable_cents": corrected_cents,
                            "delta_cents": delta,
                            "delta_exact_cents": delta_exact,
                            "old_rates_only": True,
                            "corrected_line_ids": current_line_ids.get(contract, []),
                            "corrected_lines_at_calculation": current_line_objects.get(contract, []),
                        },
                    )
                    entries.append(entry)
                state["last_corrected_receivable_cents"] = corrected_cents
                state["last_corrected_exact_cents"] = corrected_exact
        return entries

    def _make_journal_entry(self, **kwargs) -> dict[str, Any]:
        old_month = kwargs["old_month"]
        contract = kwargs["contract"]
        close_id = kwargs["close_id"]
        kind = kwargs["kind"]
        entry_id = f"J-ADJ-{old_month}-{contract}-{close_id}"
        # Deterministic suffix if the same close somehow yields repeated kinds.
        base_id = entry_id
        n = 2
        while entry_id in self.journal:
            entry_id = f"{base_id}-{n}"
            n += 1
        entry = {
            "entry_id": entry_id,
            "kind": kind,
            "status": "pending_posting",
            "for_month": old_month,
            "book_month": kwargs["target_book_month"],
            "contract": contract,
            "amount_cents": kwargs["amount_cents"],
            "amount_exact_cents": kwargs.get("amount_exact_cents", Decimal(kwargs["amount_cents"])),
            "rounding": "ROUND_HALF_UP to whole cents on each journal/invoice line",
            "created_seq": kwargs["as_of"],
            "close_id": close_id,
            "reason": kwargs["reason"],
            "basis": kwargs["basis"],
            "settling_receipt_ids": [],
            "booked_after_creation_cents": 0,
            "unposted_amount_cents": kwargs["amount_cents"],
        }
        self.journal[entry_id] = entry
        return entry

    @staticmethod
    def _receipt_ref(row: dict[str, Any]) -> dict[str, Any]:
        b = row["body"]
        return {
            "receipt_id": b["id"],
            "seq": row["seq"],
            "book_month": b.get("book_month"),
            "amount_cents": b.get("amount_cents"),
            "origin": b.get("origin"),
        }

    def refresh_journal_postings(self, as_of_seq: int | None = None) -> None:
        """Match post-creation receipts to entries.

        Explicit ``adjustment_id``/``entry_id``/``origin`` references win.
        Unlinked later receipts are allocated deterministically in entry order.
        Receipts used in an entry's calculation basis are not re-counted here.
        """
        for entry in self.journal.values():
            entry["settling_receipt_ids"] = []
            entry["booked_after_creation_cents"] = 0
            entry["unposted_amount_cents"] = int(entry["amount_cents"])
            entry["status"] = "pending_posting"

        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for entry in self.journal.values():
            groups[(entry["for_month"], entry["contract"])].append(entry)

        for (month, contract), entries in groups.items():
            entries.sort(key=lambda e: (int(e["created_seq"]), e["entry_id"]))
            baseline_seq = min(int(e["created_seq"]) for e in entries)
            future_rows = [
                row
                for row in self._posted_for(month, contract)
                if row["seq"] > baseline_seq and (as_of_seq is None or row["seq"] < as_of_seq)
            ]
            assigned_receipt_ids: set[str] = set()

            # Explicit references first.
            for row in future_rows:
                body = row["body"]
                refs = {
                    str(body.get("adjustment_id") or ""),
                    str(body.get("journal_entry_id") or ""),
                    str(body.get("entry_id") or ""),
                    str(body.get("origin") or ""),
                }
                for entry in entries:
                    if entry["entry_id"] in refs:
                        self._assign_receipt(entry, row)
                        assigned_receipt_ids.add(str(body["id"]))

            # Deterministic fallback for unlinked receipts.
            for row in future_rows:
                rid = str(row["body"]["id"])
                if rid in assigned_receipt_ids:
                    continue
                amount = int(row["body"]["amount_cents"])
                for entry in entries:
                    remaining = int(entry["amount_cents"]) - int(entry["booked_after_creation_cents"])
                    if remaining == 0 or amount == 0:
                        continue
                    if remaining > 0 and amount > 0:
                        take = min(amount, remaining)
                    elif remaining < 0 and amount < 0:
                        take = max(amount, remaining)
                    else:
                        continue
                    self._assign_receipt(entry, row, amount_override=take, append_receipt=rid not in entry["settling_receipt_ids"])
                    amount -= take
                    if amount == 0:
                        break

            for entry in entries:
                amount = int(entry["amount_cents"])
                booked = int(entry["booked_after_creation_cents"])
                entry["unposted_amount_cents"] = amount - booked
                if booked == amount:
                    entry["status"] = "posted"
                elif booked != 0:
                    entry["status"] = "partially_posted"
                else:
                    entry["status"] = "pending_posting"

    @staticmethod
    def _assign_receipt(entry: dict[str, Any], row: dict[str, Any], amount_override: int | None = None, append_receipt: bool = True) -> None:
        amount = int(row["body"]["amount_cents"]) if amount_override is None else amount_override
        entry["booked_after_creation_cents"] = int(entry.get("booked_after_creation_cents", 0)) + amount
        rid = str(row["body"]["id"])
        if append_receipt and rid not in entry["settling_receipt_ids"]:
            entry["settling_receipt_ids"].append(rid)

    # ----------------------------------------------------------------- queries
    def original_bill(self, month: str) -> dict[str, Any]:
        """Re-fetch the immutable close snapshot ("原账")."""
        if month not in self.closed:
            raise KeyError(f"month {month} has not been closed")
        return self.closed[month]

    def current_responsibility(self, meter: str | None = None) -> list[dict[str, Any]]:
        intervals = self.view()["responsibility"]
        return [x for x in intervals if meter is None or x["meter"] == meter]

    def unclaimed_ranges(self, meter: str | None = None) -> list[dict[str, Any]]:
        return [
            i for i in self.report()["admin"]["unclaimed"]
            if meter is None or i.get("meter") == meter
        ]

    def pending_ranges(self, meter: str | None = None) -> list[dict[str, Any]]:
        return [
            i for i in self.report()["admin"]["pending"]
            if meter is None or i.get("meter") == meter
        ]

    def explain_line(self, line_id: str) -> dict[str, Any] | None:
        for line in self.view()["lines"]:
            if line["line_id"] == line_id:
                return line
        if line_id in self.journal:
            return self.journal[line_id]
        for snap in self.closed.values():
            for line in snap.get("usage_lines", []):
                if line["line_id"] == line_id:
                    return line
        return None

    # ----------------------------------------------------------------- reports
    def closed_month_positions(self) -> list[dict[str, Any]]:
        self.refresh_journal_postings()
        current = self.view()
        current_cents: dict[tuple[str, str], int] = defaultdict(int)
        for line in current["lines"]:
            if line["kind"] == "usage":
                current_cents[(line["for_month"], line["contract"])] += line["invoice_amount_cents"]

        positions = []
        for month, snap in sorted(self.closed.items()):
            contracts = set(snap["receivable_by_contract_cents"])
            contracts |= {c for (m, c) in current_cents if m == month}
            contracts |= {e["contract"] for e in self.journal.values() if e["for_month"] == month}
            for contract in sorted(contracts):
                entries = [e for e in self.journal.values() if e["for_month"] == month and e["contract"] == contract]
                posted_rows = self._posted_for(month, contract)
                posted_cents = sum(int(r["body"]["amount_cents"]) for r in posted_rows)
                journal_amount = sum(int(e["amount_cents"]) for e in entries)
                unposted_amount = sum(int(e["unposted_amount_cents"]) for e in entries)
                original_cents = int(snap["receivable_by_contract_cents"].get(contract, 0))
                corrected_cents = int(current_cents.get((month, contract), original_cents))
                positions.append(
                    {
                        "for_month": month,
                        "contract": contract,
                        "close_id": snap["close_id"],
                        "original_receivable_cents": original_cents,
                        "current_old_rate_corrected_receivable_cents": corrected_cents,
                        "posted_cents": posted_cents,
                        "original_minus_posted_cents": original_cents - posted_cents,
                        "corrected_minus_posted_cents": corrected_cents - posted_cents,
                        "fact_delta_cents": corrected_cents - original_cents,
                        "journal_entry_amount_cents": journal_amount,
                        "unposted_journal_amount_cents": unposted_amount,
                        "journal_entry_ids": [e["entry_id"] for e in entries],
                        "posted_receipt_ids": [r["body"]["id"] for r in posted_rows],
                    }
                )
        return positions

    def report(self) -> dict[str, Any]:
        self.refresh_journal_postings()
        current = self.view()
        admin = {
            "unclaimed": [
                self._public_issue(i)
                for i in current["issues"]
                if i["kind"] in {"unclaimed", "unclaimed_share"}
            ],
            "pending": [
                self._public_issue(i)
                for i in current["issues"]
                if i["kind"] not in {"unclaimed", "unclaimed_share"}
            ],
            "timeline": current["timeline"],
        }
        finance = {
            "current_usage_lines": current["lines"],
            "closed_months": list(self.closed.values()),
            "closed_month_positions": self.closed_month_positions(),
            "journal_entries": list(self.journal.values()),
            "posted_receipts": [self._receipt_ref(row) for row in sorted(self.posted.values(), key=lambda r: r["seq"])],
            "reconciliation_state": self.recon_state,
        }
        return {
            "admin": admin,
            "finance": finance,
            "current": current,
            "closed_months": self.closed,
            "journal_entries": list(self.journal.values()),
            "close_attempts": self.close_attempts,
            "posted_conflicts": self.posted_conflicts,
        }

    @staticmethod
    def _public_issue(issue: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "issue_id", "kind", "blocking", "meter", "boot", "start", "end",
            "source_ids", "lease_ids", "contracts", "fact_id", "message", "unclaimed_seconds",
        )
        return {k: issue[k] for k in keys if k in issue}
