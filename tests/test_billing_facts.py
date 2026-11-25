from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from billing_facts import CloseRejected, Warehouse  # noqa: E402

DATA = ROOT / "data" / "exchanges.jsonl"
T0 = "2026-01-01T00:00:00Z"


def body(kind, **payload):
    return {"step": 1, "kind": kind, "body": payload}


class SampleFactWarehouse(unittest.TestCase):
    def setUp(self):
        self.wh = Warehouse()
        self.results = self.wh.ingest_jsonl(str(DATA))
        self.report = self.wh.report()

    def test_close_succeeds_with_exact_allowed_holds(self):
        self.assertEqual([a["status"] for a in self.wh.close_attempts], ["closed", "closed"])
        self.assertIn("2026-10", self.wh.closed)
        self.assertIn("2026-11", self.wh.closed)

    def test_overlapping_logs_are_deduplicated_and_state_priority_kept(self):
        facts = {(s["meter"], s["fact_id"].split(":")[-1]): s for s in self.report["current"]["timeline"]}
        m7_use = [s for s in self.report["current"]["timeline"] if s["meter"] == "m7" and s["state"] == "use"]
        m7_cal = [s for s in self.report["current"]["timeline"] if s["meter"] == "m7" and s["state"] == "cal"]
        self.assertEqual(sum(s["seconds"] for s in m7_use), 170)
        self.assertEqual(sum(s["seconds"] for s in m7_cal), 10)
        self.assertEqual(sum(s["seconds"] for s in self.report["current"]["timeline"] if s["meter"] == "m2"), 100)
        # l2 is corroborating overlap, not an extra 80 seconds.
        self.assertIn("l2", m7_use[0]["source_ids"] + m7_use[1]["source_ids"])

    def test_current_dedicated_collision_is_one_preserved_nineteen_second_range(self):
        conflicts = [i for i in self.report["current"]["issues"] if i["kind"] == "lease_conflict"]
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict["start"], "2026-10-31T23:59:54Z")
        self.assertEqual(conflict["end"], "2026-11-01T00:00:13Z")
        self.assertEqual(conflict["lease_ids"], ["a7", "b7"])

    def test_original_october_bill_uses_old_clock_and_old_free_order(self):
        oct_close = self.wh.closed["2026-10"]
        self.assertEqual(oct_close["receivable_by_contract_cents"], {"A": 41, "B": 9})
        a_line = next(l for l in oct_close["usage_lines"] if l["contract"] == "A")
        free_by_meter: dict[str, Decimal] = {}
        for c in a_line["coverage"]:
            free_by_meter[c["meter"]] = free_by_meter.get(c["meter"], Decimal("0")) + c["free_seconds"]
        self.assertEqual(free_by_meter["m7"], Decimal("29"))
        self.assertEqual(free_by_meter["m2"], Decimal("21"))
        self.assertEqual(a_line["exact_amount_cents"], Decimal("41.250"))

    def test_corrected_october_quota_changes_source_occupancy_but_uses_old_rate(self):
        oct_a = next(
            l for l in self.report["current"]["lines"]
            if l["for_month"] == "2026-10" and l["contract"] == "A"
        )
        self.assertEqual(oct_a["invoice_amount_cents"], 15)
        free_by_meter = {c["meter"]: c["free_seconds"] for c in oct_a["coverage"]}
        self.assertEqual(free_by_meter["m2"], Decimal("26.4"))
        self.assertEqual(free_by_meter["m7"], Decimal("23.6"))
        self.assertTrue(all("2026-10-01" in l["rate"]["start"] for l in [oct_a]))

    def test_november_invoice_lines(self):
        nov = self.wh.closed["2026-11"]
        self.assertEqual(nov["receivable_by_contract_cents"], {"A": 179, "B": 6})

    def test_traceable_net_adjustment_and_closed_month_position(self):
        ids = {e["entry_id"]: e for e in self.report["journal_entries"]}
        adjustment = ids["J-ADJ-2026-10-A-nov"]
        self.assertEqual(adjustment["kind"], "closed_month_reconciliation")
        self.assertEqual(adjustment["amount_cents"], 25)
        self.assertEqual(adjustment["status"], "pending_posting")
        self.assertEqual(adjustment["basis"]["memo_booking_gap_original_minus_posted_cents"], 51)
        self.assertEqual(adjustment["basis"]["memo_fact_correction_corrected_minus_original_cents"], -26)
        position = next(
            p for p in self.report["finance"]["closed_month_positions"]
            if p["for_month"] == "2026-10" and p["contract"] == "A"
        )
        self.assertEqual(position["posted_cents"], -10)
        self.assertEqual(position["corrected_minus_posted_cents"], 25)
        self.assertEqual(position["journal_entry_amount_cents"], 25)
        self.assertEqual(position["unposted_journal_amount_cents"], 25)

    def test_duplicate_receipt_id_is_counted_once(self):
        receipt = body(
            "posted", id="erp-631", contract="A", for_month="2026-10",
            book_month="2026-11", amount_cents=-10, origin="external-invoice-6",
        )
        self.wh.ingest_record(receipt)
        self.wh.ingest_record(receipt)
        position = next(
            p for p in self.wh.report()["finance"]["closed_month_positions"]
            if p["for_month"] == "2026-10" and p["contract"] == "A"
        )
        self.assertEqual(position["posted_cents"], -10)
        self.assertEqual(position["corrected_minus_posted_cents"], 25)

    def test_later_close_does_not_repeat_closed_month_difference(self):
        before = [e["entry_id"] for e in self.wh.journal.values() if e["for_month"] == "2026-10"]
        self.wh.close_month({"id": "dec", "month": "2026-12", "allowed_holds": []})
        after = [e["entry_id"] for e in self.wh.journal.values() if e["for_month"] == "2026-10"]
        self.assertEqual(before, after)
        self.assertEqual(len([e for e in self.wh.journal.values() if e["for_month"] == "2026-10"]), 2)

    def test_receipt_distinguishes_posted_and_unposted(self):
        entry_id = "J-ADJ-2026-10-A-nov"
        self.wh.ingest_record(body(
            "posted", id="erp-adjust-25", contract="A", for_month="2026-10",
            book_month="2026-12", amount_cents=25,
            adjustment_id=entry_id,
        ))
        report = self.wh.report()
        entry = next(e for e in report["journal_entries"] if e["entry_id"] == entry_id)
        self.assertEqual(entry["booked_after_creation_cents"], 25)
        self.assertEqual(entry["unposted_amount_cents"], 0)
        self.assertEqual(entry["status"], "posted")


class EngineRules(unittest.TestCase):
    def _base(self):
        wh = Warehouse()
        wh.ingest_record(body("clock", meter="m", boot="b", zero=T0, from_tick=0, to_tick=1000, rev=1))
        wh.ingest_record(body("rate", contract="C", start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z",
                              cents_per_sec="1", free_seconds=0))
        return wh

    def test_state_priority_and_overlap_dedup(self):
        wh = self._base()
        for payload in [
            dict(id="l1", meter="m", boot="b", start=0, end=100, state="use", rev=1),
            dict(id="l2", meter="m", boot="b", start=20, end=30, state="cal", rev=1),
            dict(id="l3", meter="m", boot="b", start=40, end=60, state="fault", rev=1),
            dict(id="l4", meter="m", boot="b", start=50, end=55, state="cal", rev=1),
        ]:
            wh.ingest_record(body("log", **payload))
        states = [(s["tick_start"], s["tick_end"], s["state"]) for s in wh.view()["timeline"]]
        self.assertIn((0, 20, "use"), states)
        self.assertIn((20, 30, "cal"), states)
        self.assertIn((40, 60, "fault"), states)
        use_seconds = sum(e - s for s, e, state in states if state == "use")
        self.assertEqual(use_seconds, 70)

    def test_unclaimed_usage_is_preserved_and_queryable(self):
        wh = self._base()
        wh.ingest_record(body("log", id="l", meter="m", boot="b", start=0, end=10, state="use", rev=1))
        view = wh.view()
        resp = view["responsibility"][0]
        self.assertEqual(resp["status"], "unclaimed")
        self.assertEqual(resp["seconds"], 10)
        self.assertEqual(view["lines"], [])

    def test_conflict_blocks_close_unless_allowed_hold_covers_month_part(self):
        wh = self._base()
        wh.ingest_record(body("log", id="l", meter="m", boot="b", start=0, end=10, state="use", rev=1))
        wh.ingest_record(body("lease", id="a", meter="m", contract="C", start=T0, end="2026-02-01T00:00:00Z",
                              share=100, group=None, rev=1))
        wh.ingest_record(body("lease", id="d", meter="m", contract="D", start=T0, end="2026-02-01T00:00:00Z",
                              share=100, group=None, rev=1))
        with self.assertRaises(CloseRejected):
            wh.close_month(body("close", id="bad", month="2026-01", allowed_holds=[])["body"])
        snap = wh.close_month({
            "id": "ok", "month": "2026-01",
            "allowed_holds": [["m", T0, "2026-01-01T00:00:10Z"]],
        })
        self.assertEqual(snap["status"], "closed")

    def test_repair_confirmation_can_classify_fault_wait_as_chargeable_use(self):
        wh = self._base()
        wh.ingest_record(body("log", id="f", meter="m", boot="b", start=0, end=10, state="fault", rev=1))
        wh.ingest_record(body("lease", id="a", meter="m", contract="C", start=T0, end="2026-02-01T00:00:00Z",
                              share=100, group=None, rev=1))
        self.assertEqual(wh.view()["lines"], [])
        wh.ingest_record(body(
            "repair_confirmation", id="r1", meter="m", start=T0, end="2026-01-01T00:00:10Z",
            determination="tenant_fault_billable_wait",
        ))
        line = wh.view()["lines"][0]
        self.assertEqual(line["billable_seconds"], Decimal("10"))
        self.assertEqual(line["invoice_amount_cents"], 10)

    def test_shared_allocation_is_exact(self):
        wh = self._base()
        wh.ingest_record(body("log", id="l", meter="m", boot="b", start=0, end=100, state="use", rev=1))
        wh.ingest_record(body("lease", id="a", meter="m", contract="C", start=T0, end="2026-02-01T00:00:00Z",
                              share=100, group="g", rev=1))
        # Same group, another 50% would total 150 and be invalid; use 50/50.
        wh.ingest_record(body("lease", id="d", meter="m", contract="D", start=T0, end="2026-02-01T00:00:00Z",
                              share=50, group="g", rev=1))
        # Replace A with 50 through a higher revision.
        wh.ingest_record(body("lease", id="a", meter="m", contract="C", start=T0, end="2026-02-01T00:00:00Z",
                              share=50, group="g", rev=2))
        resp = wh.view()["responsibility"][0]
        self.assertEqual(resp["status"], "allocated")
        self.assertEqual([a["allocated_seconds"] for a in resp["allocations"]], [Decimal("50.0"), Decimal("50")])


if __name__ == "__main__":
    unittest.main()
