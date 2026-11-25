"""Command line entry point."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import Warehouse
from .report import admin_markdown, finance_markdown, html_report
from .sqlite_store import write_sqlite
from .timeutil import decimal_json_default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build queryable usage/billing facts from exchange JSONL")
    p.add_argument("--input", "-i", action="append", default=[], help="exchange JSONL file(s), in delivery order")
    p.add_argument("--outdir", "-o", default=str(Path(__file__).resolve().parent.parent / "output"), help="directory for JSON/Markdown/SQLite outputs")
    p.add_argument("--db-name", default="warehouse.sqlite")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parent.parent
    inputs = args.input or [str(project_root / "data" / "exchanges.jsonl")]
    wh = Warehouse()
    ingest_results = []
    for name in inputs:
        ingest_results.extend(wh.ingest_jsonl(name))

    report = wh.report()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=decimal_json_default) + "\n",
        encoding="utf-8",
    )
    (outdir / "admin.md").write_text(admin_markdown(report), encoding="utf-8")
    (outdir / "finance.md").write_text(finance_markdown(report), encoding="utf-8")
    (outdir / "index.html").write_text(html_report(report), encoding="utf-8")
    key_results = {
        "closed_months": {
            month: {
                "close_id": snap["close_id"],
                "original_receivable_by_contract_cents": snap["receivable_by_contract_cents"],
                "adjustment_net_by_contract_cents": snap.get("prior_month_adjustment_net_by_contract_cents", {}),
                "close_period_total_by_contract_cents": snap.get("close_period_invoice_total_by_contract_cents", {}),
            }
            for month, snap in report["closed_months"].items()
        },
        "current_invoice_lines": [
            {
                "line_id": line["line_id"],
                "contract": line["contract"],
                "for_month": line["for_month"],
                "invoice_amount_cents": line["invoice_amount_cents"],
                "exact_amount_cents": line["exact_amount_cents"],
                "billable_seconds": line["billable_seconds"],
            }
            for line in report["current"]["lines"]
        ],
        "closed_month_positions": report["finance"]["closed_month_positions"],
        "journal_entries": [
            {
                "entry_id": e["entry_id"],
                "kind": e["kind"],
                "for_month": e["for_month"],
                "book_month": e["book_month"],
                "contract": e["contract"],
                "amount_cents": e["amount_cents"],
                "booked_after_creation_cents": e["booked_after_creation_cents"],
                "unposted_amount_cents": e["unposted_amount_cents"],
                "status": e["status"],
            }
            for e in report["journal_entries"]
        ],
        "unclaimed": report["admin"]["unclaimed"],
        "pending": report["admin"]["pending"],
    }
    (outdir / "key-results.json").write_text(
        json.dumps(key_results, ensure_ascii=False, indent=2, default=decimal_json_default) + "\n",
        encoding="utf-8",
    )
    write_sqlite(wh, report, outdir / args.db_name)

    rejected = [r for r in ingest_results if r.get("rejected")]
    print(f"inputs={len(inputs)} rows={len(wh.rows)} closed={sorted(wh.closed)} journal={len(wh.journal)} rejected_closes={len(rejected)}")
    print(f"wrote report.json, key-results.json, admin.md, finance.md, index.html, {args.db_name} in {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
