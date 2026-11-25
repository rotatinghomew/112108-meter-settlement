"""Small SQLite projection of the fact warehouse."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .timeutil import decimal_json_default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=decimal_json_default, sort_keys=True)


def write_sqlite(warehouse, report: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        cur = con.cursor()
        cur.executescript(
            """
            create table raw_exchange(seq integer primary key, step integer, kind text, body_json text not null);
            create table usage_fact(fact_id text primary key, meter text, boot text, start_utc text, end_utc text,
                                    seconds integer, state text, effective_use integer, source_id text, source_ids_json text);
            create table issue(issue_id text primary key, kind text, blocking integer, meter text,
                               start_utc text, end_utc text, payload_json text);
            create table responsibility(responsibility_id text primary key, fact_id text, meter text,
                                        start_utc text, end_utc text, state text, effective_use integer,
                                        status text, contracts_json text, payload_json text);
            create table bill_line(line_id text primary key, kind text, contract text, for_month text,
                                   book_month text, invoice_amount_cents integer, exact_amount_cents text,
                                   billable_seconds text, payload_json text);
            create table bill_coverage(line_id text, fact_id text, meter text, source_id text,
                                       start_utc text, end_utc text, allocated_seconds text,
                                       free_seconds text, billable_seconds text);
            create table close_snapshot(month text primary key, close_id text, snapshot_json text not null);
            create table close_bill_line(close_id text, month text, line_id text, contract text,
                                         invoice_amount_cents integer, payload_json text,
                                         primary key(close_id, line_id));
            create table journal_entry(entry_id text primary key, for_month text, book_month text,
                                       contract text, kind text, amount_cents integer,
                                       unposted_amount_cents integer, status text, payload_json text);
            create table posted_receipt(receipt_id text primary key, for_month text, book_month text,
                                        contract text, amount_cents integer, origin text, payload_json text);
            """
        )
        for row in warehouse.rows:
            cur.execute(
                "insert into raw_exchange values (?,?,?,?)",
                (row["seq"], row.get("step"), row["kind"], _dumps(row["body"])),
            )
        for fact in report["current"]["timeline"]:
            cur.execute(
                "insert or replace into usage_fact values (?,?,?,?,?,?,?,?,?,?)",
                (
                    fact["fact_id"], fact["meter"], fact["boot"], fact["start"], fact["end"],
                    fact["seconds"], fact["state"], int(fact["effective_use"]), fact.get("source_id"),
                    _dumps(fact["source_ids"]),
                ),
            )
        for issue in report["current"]["issues"]:
            cur.execute(
                "insert or replace into issue values (?,?,?,?,?,?,?)",
                (
                    issue["issue_id"], issue["kind"], int(bool(issue.get("blocking"))), issue.get("meter"),
                    issue.get("start"), issue.get("end"), _dumps(issue),
                ),
            )
        for resp in report["current"]["responsibility"]:
            cur.execute(
                "insert or replace into responsibility values (?,?,?,?,?,?,?,?,?,?)",
                (
                    resp["responsibility_id"], resp["fact_id"], resp["meter"], resp["start"], resp["end"],
                    resp["state"], int(resp["effective_use"]), resp["status"],
                    _dumps(resp.get("contracts", [])), _dumps(resp),
                ),
            )
        for line in report["current"]["lines"]:
            cur.execute(
                "insert or replace into bill_line values (?,?,?,?,?,?,?,?,?)",
                (
                    line["line_id"], line["kind"], line["contract"], line["for_month"], line["book_month"],
                    line["invoice_amount_cents"], str(line["exact_amount_cents"]), str(line["billable_seconds"]),
                    _dumps(line),
                ),
            )
            for cov in line["coverage"]:
                cur.execute(
                    "insert into bill_coverage values (?,?,?,?,?,?,?,?,?)",
                    (
                        line["line_id"], cov["fact_id"], cov["meter"], cov.get("source_id"),
                        cov["start"], cov["end"], str(cov["allocated_seconds"]),
                        str(cov["free_seconds"]), str(cov["billable_seconds"]),
                    ),
                )
        for month, snap in report["closed_months"].items():
            cur.execute("insert or replace into close_snapshot values (?,?,?)", (month, snap["close_id"], _dumps(snap)))
            for line in snap.get("usage_lines", []):
                cur.execute(
                    "insert or replace into close_bill_line values (?,?,?,?,?,?)",
                    (
                        snap["close_id"], month, line["line_id"], line["contract"],
                        line["invoice_amount_cents"], _dumps(line),
                    ),
                )
        for entry in report["journal_entries"]:
            cur.execute(
                "insert or replace into journal_entry values (?,?,?,?,?,?,?,?,?)",
                (
                    entry["entry_id"], entry["for_month"], entry["book_month"], entry["contract"],
                    entry["kind"], entry["amount_cents"], entry["unposted_amount_cents"], entry["status"], _dumps(entry),
                ),
            )
        for rid, row in warehouse.posted.items():
            body = row["body"]
            cur.execute(
                "insert or replace into posted_receipt values (?,?,?,?,?,?,?)",
                (
                    rid, body.get("for_month"), body.get("book_month"), body.get("contract"),
                    int(body["amount_cents"]), body.get("origin"), _dumps(body),
                ),
            )
        con.commit()
    finally:
        con.close()
