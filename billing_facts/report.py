"""Human-readable audit reports."""
from __future__ import annotations

import html
import json
from typing import Any


def _cents(value: Any) -> str:
    return str(value)


def admin_markdown(report: dict[str, Any]) -> str:
    current = report["current"]
    lines = ["# 设备管理员：统一时间事实", "", "## 状态时间线（UTC，半开区间）", ""]
    lines.append("| 设备 | 启动 | 开始 | 结束 | 秒 | 状态 | 有效用量 | 采用源 | 重叠/佐证源 |")
    lines.append("|---|---|---|---|---:|---|---|---|---|")
    for s in current["timeline"]:
        lines.append(
            f"| {s['meter']} | {s['boot']} | {s['start']} | {s['end']} | {s['seconds']} | "
            f"{s['state']} | {'是' if s['effective_use'] else '否'} | {s.get('source_id') or ''} | "
            f"{', '.join(s['source_ids'])} |"
        )

    lines += ["", "## 未认领有效用量", ""]
    unclaimed = [i for i in current["issues"] if i["kind"] in {"unclaimed", "unclaimed_share"}]
    if not unclaimed:
        lines.append("无。")
    for i in unclaimed:
        lines.append(f"- `{i['meter']}` {i['start']} → {i['end']} `{i['kind']}` {i.get('contracts') or ''}")

    lines += ["", "## 待查/冲突区间", ""]
    pending = [i for i in current["issues"] if i.get("blocking")]
    if not pending:
        lines.append("无。")
    for i in pending:
        lines.append(
            f"- `{i['meter']}` {i.get('start')} → {i.get('end')} `{i['kind']}` "
            f"sources={i.get('source_ids', [])} leases={i.get('lease_ids', [])} — {i.get('message', '')}"
        )
    return "\n".join(lines) + "\n"


def finance_markdown(report: dict[str, Any]) -> str:
    lines = ["# 财务：账单、原账与调账分录", ""]

    lines += ["## 当前有效用量账单行", ""]
    lines.append("| 账单行 | 合同 | 费用月 | 入账月 | 分摊秒 | 免费秒 | 计费秒 | 精确金额(分) | 发票整数分 |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
    for line in report["current"]["lines"]:
        lines.append(
            f"| {line['line_id']} | {line['contract']} | {line['for_month']} | {line['book_month']} | "
            f"{line['exact_allocated_seconds']} | {line['free_seconds']} | {line['billable_seconds']} | "
            f"{line['exact_amount_cents']} | {line['invoice_amount_cents']} |"
        )

    lines += ["", "## 已封账月份原账（不被重跑替换）", ""]
    for month, snap in sorted(report["closed_months"].items()):
        lines.append(f"### {month}（close `{snap['close_id']}`）")
        lines.append("")
        lines.append("| 合同 | 原账发票金额(分) | 精确应收(分) |")
        lines.append("|---|---:|---:|")
        for contract, cents in sorted(snap["receivable_by_contract_cents"].items()):
            exact = snap["receivable_exact_by_contract"].get(contract, "")
            lines.append(f"| {contract} | {cents} | {exact} |")
        lines.append("")

    lines += ["## 封账月对账位置", ""]
    positions = report["finance"].get("closed_month_positions", [])
    if not positions:
        lines.append("无。")
    else:
        lines.append("| 费用月 | 合同 | 原账(分) | 更正后旧月应收(分) | 已入账回执(分) | 原账-已入账 | 更正应收-已入账 | 分录净额 | 尚未入账分录净额 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for p in positions:
            lines.append(
                f"| {p['for_month']} | {p['contract']} | {p['original_receivable_cents']} | "
                f"{p['current_old_rate_corrected_receivable_cents']} | {p['posted_cents']} | "
                f"{p['original_minus_posted_cents']} | {p['corrected_minus_posted_cents']} | "
                f"{p['journal_entry_amount_cents']} | {p['unposted_journal_amount_cents']} |"
            )
        lines.append("")

    lines += ["## 调账/补记分录", ""]
    if not report["journal_entries"]:
        lines.append("无。")
    else:
        lines.append("| 分录 | 类型 | 费用月 | 入账月 | 合同 | 金额(分) | 封账后已关联入账(分) | 未入账差额(分) | 状态 |")
        lines.append("|---|---|---|---|---|---:|---:|---:|---|")
        for e in report["journal_entries"]:
            lines.append(
                f"| {e['entry_id']} | {e['kind']} | {e['for_month']} | {e['book_month']} | {e['contract']} | "
                f"{e['amount_cents']} | {e['booked_after_creation_cents']} | {e['unposted_amount_cents']} | {e['status']} |"
            )
        lines.append("")
        for e in report["journal_entries"]:
            lines.append(f"### `{e['entry_id']}`")
            lines.append(f"- 性质：{e['reason']}")
            basis = json.dumps(e["basis"], ensure_ascii=False, indent=2, default=str)
            lines.append("- 依据：")
            lines.append("")
            lines.append("```json")
            lines.append(basis)
            lines.append("```")
            if e.get("settling_receipt_ids"):
                lines.append(f"- 已关联回执：{', '.join(e['settling_receipt_ids'])}")
            lines.append("")

    lines += ["## 已入账回执", ""]
    for receipt in report["finance"]["posted_receipts"]:
        lines.append(
            f"- `{receipt['receipt_id']}` {receipt['amount_cents']} 分，费用月见源记录，"
            f"入账月 {receipt['book_month']}，来源 `{receipt['origin']}`"
        )
    return "\n".join(lines) + "\n"


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def html_report(report: dict[str, Any]) -> str:
    timeline_rows = []
    for s in report["current"]["timeline"]:
        timeline_rows.append([
            _e(s["meter"]), _e(s["boot"]), _e(s["start"]), _e(s["end"]), s["seconds"],
            _e(s["state"]), "是" if s["effective_use"] else "否", _e(s.get("source_id")),
            _e(", ".join(s["source_ids"])),
        ])

    issue_rows = []
    for i in report["current"]["issues"]:
        nature = "阻断封账" if i.get("blocking") else (
            "未认领" if i["kind"] in {"unclaimed", "unclaimed_share"} else "非计费/待核对"
        )
        issue_rows.append([
            _e(i["kind"]), _e(i.get("meter")), _e(i.get("start")), _e(i.get("end")),
            nature,
            _e(", ".join(i.get("lease_ids", []))), _e(i.get("message")),
        ])

    line_rows = []
    for line in report["current"]["lines"]:
        coverage = "<br>".join(
            f"{_e(c['meter'])} {_e(c['start'])}→{_e(c['end'])} "
            f"alloc={_e(c['allocated_seconds'])} free={_e(c['free_seconds'])}"
            for c in line["coverage"]
        )
        line_rows.append([
            _e(line["line_id"]), _e(line["contract"]), _e(line["for_month"]),
            _e(line["book_month"]), _e(line["exact_allocated_seconds"]),
            _e(line["free_seconds"]), _e(line["billable_seconds"]),
            _e(line["exact_amount_cents"]), line["invoice_amount_cents"], coverage,
        ])

    position_rows = []
    for p in report["finance"].get("closed_month_positions", []):
        position_rows.append([
            _e(p["for_month"]), _e(p["contract"]), p["original_receivable_cents"],
            p["current_old_rate_corrected_receivable_cents"], p["posted_cents"],
            p["original_minus_posted_cents"], p["corrected_minus_posted_cents"],
            p["journal_entry_amount_cents"], p["unposted_journal_amount_cents"],
        ])

    journal_blocks = []
    for entry in report["journal_entries"]:
        basis = json.dumps(entry["basis"], ensure_ascii=False, indent=2, default=str)
        journal_blocks.append(
            f"<section class='card'><h3>{_e(entry['entry_id'])} "
            f"<span class='badge {_e(entry['status'])}'>{_e(entry['status'])}</span></h3>"
            f"<p>{_e(entry['reason'])}</p>"
            f"<p><b>{_e(entry['amount_cents'])} 分</b>；封账后已关联入账 "
            f"{_e(entry['booked_after_creation_cents'])}；未入账 "
            f"<b>{_e(entry['unposted_amount_cents'])}</b></p>"
            f"<pre>{_e(basis)}</pre></section>"
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>共享仪器计费事实仓</title>
<style>
:root {{ color-scheme: light; }}
body {{ font-family: -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif; margin: 2rem; color:#17202a; background:#f7f9fb; }}
h1,h2,h3 {{ line-height:1.35; }}
table {{ border-collapse:collapse; width:100%; background:white; margin:.75rem 0 1.5rem; font-size:.9rem; }}
th,td {{ border:1px solid #d9e1e8; padding:.45rem .55rem; vertical-align:top; text-align:left; }}
th {{ background:#eef3f7; }}
.card {{ background:white; border:1px solid #d9e1e8; border-radius:10px; padding:1rem; margin:1rem 0; }}
.badge {{ border-radius:999px; padding:.1rem .55rem; font-size:.78rem; background:#fff3cd; }}
.badge.posted {{ background:#d1e7dd; }} .badge.partially_posted {{ background:#cfe2ff; }}
pre {{ background:#102030; color:#e8eef4; padding:1rem; border-radius:8px; overflow:auto; font-size:.78rem; }}
.muted {{ color:#5b6b7b; }}
</style></head><body>
<h1>共享仪器计费事实与封账更正</h1>
<p class="muted">UTC 半开区间；精确分量见 JSON/SQLite。本页由交换资料离线生成。</p>
<h2>设备管理员：统一时间与待查</h2>
{_table(["设备","启动","开始","结束","秒","状态","有效用量","采用源","佐证源"], timeline_rows)}
<h3>未认领 / 待查区间</h3>
{_table(["类型","设备","开始","结束","性质","租约","说明"], issue_rows)}
<h2>财务：账单行与覆盖依据</h2>
{_table(["账单行","合同","费用月","入账月","分摊秒","免费秒","计费秒","精确金额(分)","发票(分)","覆盖与额度"], line_rows)}
<h2>封账月对账</h2>
{_table(["费用月","合同","原账","更正后旧月应收","已入账","原账-已入账","更正应收-已入账","分录净额","未入账净额"], position_rows)}
<h2>待记 / 已记分录</h2>
{''.join(journal_blocks) or '<p>无。</p>'}
</body></html>
"""

