# 财务：账单、原账与调账分录

## 当前有效用量账单行

| 账单行 | 合同 | 费用月 | 入账月 | 分摊秒 | 免费秒 | 计费秒 | 精确金额(分) | 发票整数分 |
|---|---|---|---|---:|---:|---:|---:|---:|
| LINE-2026-10-A-20261001T000000Z-20261101T000000Z | A | 2026-10 | 2026-10 | 62.0 | 50.0 | 12.0 | 15.000 | 15 |
| LINE-2026-10-B-20261001T000000Z-20270101T000000Z | B | 2026-10 | 2026-10 | 22.0 | 10.0 | 12.0 | 9.000 | 9 |
| LINE-2026-11-A-20261101T000000Z-20270101T000000Z | A | 2026-11 | 2026-11 | 149.0 | 30.0 | 119.0 | 178.500 | 179 |
| LINE-2026-11-B-20261001T000000Z-20270101T000000Z | B | 2026-11 | 2026-11 | 18.0 | 10.0 | 8.0 | 6.000 | 6 |

## 已封账月份原账（不被重跑替换）

### 2026-10（close `oct`）

| 合同 | 原账发票金额(分) | 精确应收(分) |
|---|---:|---:|
| A | 41 | 41.250 |
| B | 9 | 9.000 |

### 2026-11（close `nov`）

| 合同 | 原账发票金额(分) | 精确应收(分) |
|---|---:|---:|
| A | 179 | 178.500 |
| B | 6 | 6.000 |

## 封账月对账位置

| 费用月 | 合同 | 原账(分) | 更正后旧月应收(分) | 已入账回执(分) | 原账-已入账 | 更正应收-已入账 | 分录净额 | 尚未入账分录净额 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-10 | A | 41 | 15 | -10 | 51 | 25 | 25 | 25 |
| 2026-10 | B | 9 | 9 | 0 | 9 | 9 | 9 | 9 |
| 2026-11 | A | 179 | 179 | 0 | 179 | 179 | 0 | 0 |
| 2026-11 | B | 6 | 6 | 0 | 6 | 6 | 0 | 0 |

## 调账/补记分录

| 分录 | 类型 | 费用月 | 入账月 | 合同 | 金额(分) | 封账后已关联入账(分) | 未入账差额(分) | 状态 |
|---|---|---|---|---|---:|---:|---:|---|
| J-ADJ-2026-10-A-nov | closed_month_reconciliation | 2026-10 | 2026-11 | A | 25 | 0 | 25 | pending_posting |
| J-ADJ-2026-10-B-nov | closed_month_reconciliation | 2026-10 | 2026-11 | B | 9 | 0 | 9 | pending_posting |

### `J-ADJ-2026-10-A-nov`
- 性质：Compare the corrected complete receivable for the original month with amounts actually posted; post this signed supplement/reversal in the next open book month.
- 依据：

```json
{
  "original_close_id": "oct",
  "original_receivable_cents": 41,
  "original_exact_receivable_cents": "41.250",
  "corrected_receivable_cents": 15,
  "corrected_exact_receivable_cents": "15.000",
  "posted_cents_at_calculation": -10,
  "net_amount_cents": 25,
  "net_exact_before_line_rounding_cents": "25.000",
  "memo_booking_gap_original_minus_posted_cents": 51,
  "memo_fact_correction_corrected_minus_original_cents": -26,
  "memo_components": [
    {
      "component": "original_account_vs_posted",
      "amount_cents": 51,
      "meaning": "Would make posted ledger equal the original close account."
    },
    {
      "component": "closed_month_fact_correction",
      "amount_cents": -26,
      "meaning": "Old-rate correction from original account to current old-month recomputation."
    }
  ],
  "old_rates_only": true,
  "posted_receipts": [
    {
      "receipt_id": "erp-631",
      "seq": 17,
      "book_month": "2026-11",
      "amount_cents": -10,
      "origin": "external-invoice-6"
    }
  ],
  "original_line_ids": [
    "LINE-2026-10-A-20261001T000000Z-20261101T000000Z"
  ],
  "corrected_line_ids": [
    "LINE-2026-10-A-20261001T000000Z-20261101T000000Z"
  ],
  "corrected_lines_at_calculation": [
    {
      "line_id": "LINE-2026-10-A-20261001T000000Z-20261101T000000Z",
      "kind": "usage",
      "contract": "A",
      "for_month": "2026-10",
      "book_month": "2026-10",
      "rate": {
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-11-01T00:00:00Z",
        "cents_per_sec": "1.25"
      },
      "exact_allocated_seconds": "62.0",
      "free_seconds": "50.0",
      "billable_seconds": "12.0",
      "exact_amount_cents": "15.000",
      "invoice_amount_cents": 15,
      "coverage": [
        {
          "atom_id": "atom:resp:b3:0-100:1793491145-1793491245:a2:1793491145-1793491200",
          "fact_id": "fact:m2:b3:0-100",
          "meter": "m2",
          "source_id": "l4",
          "source_ids": [
            "l4"
          ],
          "source_coverage": [
            {
              "source_id": "l4",
              "start": "2026-10-31T23:59:05Z",
              "end": "2026-11-01T00:00:45Z"
            }
          ],
          "lease_id": "a2",
          "start": "2026-10-31T23:59:05Z",
          "end": "2026-11-01T00:00:00Z",
          "device_seconds": 55,
          "share_percent": "60",
          "allocated_seconds": "33.0",
          "free_seconds": "26.4",
          "billable_seconds": "6.6"
        },
        {
          "atom_id": "atom:resp:b9:0-70:1793491165-1793491194:a7:1793491165-1793491194",
          "fact_id": "fact:m7:b9:0-70",
          "meter": "m7",
          "source_id": "l1",
          "source_ids": [
            "l1",
            "l2"
          ],
          "source_coverage": [
            {
              "source_id": "l1",
              "start": "2026-10-31T23:59:25Z",
              "end": "2026-11-01T00:00:35Z"
            },
            {
              "source_id": "l2",
              "start": "2026-10-31T23:59:55Z",
              "end": "2026-11-01T00:00:35Z"
            }
          ],
          "lease_id": "a7",
          "start": "2026-10-31T23:59:25Z",
          "end": "2026-10-31T23:59:54Z",
          "device_seconds": 29,
          "share_percent": "100",
          "allocated_seconds": "29",
          "free_seconds": "23.6",
          "billable_seconds": "5.4"
        }
      ],
      "basis": [
        "half-open [start,end); overlapping logs deduplicated by state priority",
        "lease shares retained as exact Decimal fractions",
        "free quota ordered by occurrence second, meter, source id",
        "line total rounded once with ROUND_HALF_UP to whole cents"
      ]
    }
  ]
}
```

### `J-ADJ-2026-10-B-nov`
- 性质：Compare the corrected complete receivable for the original month with amounts actually posted; post this signed supplement/reversal in the next open book month.
- 依据：

```json
{
  "original_close_id": "oct",
  "original_receivable_cents": 9,
  "original_exact_receivable_cents": "9.000",
  "corrected_receivable_cents": 9,
  "corrected_exact_receivable_cents": "9.000",
  "posted_cents_at_calculation": 0,
  "net_amount_cents": 9,
  "net_exact_before_line_rounding_cents": "9.000",
  "memo_booking_gap_original_minus_posted_cents": 9,
  "memo_fact_correction_corrected_minus_original_cents": 0,
  "memo_components": [
    {
      "component": "original_account_vs_posted",
      "amount_cents": 9,
      "meaning": "Would make posted ledger equal the original close account."
    },
    {
      "component": "closed_month_fact_correction",
      "amount_cents": 0,
      "meaning": "Old-rate correction from original account to current old-month recomputation."
    }
  ],
  "old_rates_only": true,
  "posted_receipts": [],
  "original_line_ids": [
    "LINE-2026-10-B-20261001T000000Z-20270101T000000Z"
  ],
  "corrected_line_ids": [
    "LINE-2026-10-B-20261001T000000Z-20270101T000000Z"
  ],
  "corrected_lines_at_calculation": [
    {
      "line_id": "LINE-2026-10-B-20261001T000000Z-20270101T000000Z",
      "kind": "usage",
      "contract": "B",
      "for_month": "2026-10",
      "book_month": "2026-10",
      "rate": {
        "start": "2026-10-01T00:00:00Z",
        "end": "2027-01-01T00:00:00Z",
        "cents_per_sec": "0.75"
      },
      "exact_allocated_seconds": "22.0",
      "free_seconds": "10.0",
      "billable_seconds": "12.0",
      "exact_amount_cents": "9.000",
      "invoice_amount_cents": 9,
      "coverage": [
        {
          "atom_id": "atom:resp:b3:0-100:1793491145-1793491245:b2:1793491145-1793491200",
          "fact_id": "fact:m2:b3:0-100",
          "meter": "m2",
          "source_id": "l4",
          "source_ids": [
            "l4"
          ],
          "source_coverage": [
            {
              "source_id": "l4",
              "start": "2026-10-31T23:59:05Z",
              "end": "2026-11-01T00:00:45Z"
            }
          ],
          "lease_id": "b2",
          "start": "2026-10-31T23:59:05Z",
          "end": "2026-11-01T00:00:00Z",
          "device_seconds": 55,
          "share_percent": "40",
          "allocated_seconds": "22.0",
          "free_seconds": "10.0",
          "billable_seconds": "12.0"
        }
      ],
      "basis": [
        "half-open [start,end); overlapping logs deduplicated by state priority",
        "lease shares retained as exact Decimal fractions",
        "free quota ordered by occurrence second, meter, source id",
        "line total rounded once with ROUND_HALF_UP to whole cents"
      ]
    }
  ]
}
```

## 已入账回执

- `erp-631` -10 分，费用月见源记录，入账月 2026-11，来源 `external-invoice-6`
