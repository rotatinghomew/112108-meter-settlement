# 共享仪器的计费事实仓与封账更正

共享仪器计量、租约、费率、封账和回执的离线事实仓。时间一律 UTC，区间均为半开
`[start, end)`；费用、设备号和课题组均为合成资料。

## 运行

仅使用 Python 3.11 标准库。

```sh
python3 -m unittest discover -s tests -v
python3 solution.py --input data/exchanges.jsonl --outdir output
```

也可把后续批次按交付顺序依次传入：

```sh
python3 solution.py --input data/batch-001.jsonl --input data/batch-002.jsonl
```

产物：

- `output/report.json`：完整事实、原账、现行责任、账单覆盖、待查区间和调账分录；
- `output/key-results.json`：上述结果的精简索引；
- `output/admin.md`：设备管理员视角的统一时间、未认领和待查区间；
- `output/finance.md`：财务视角的原账、对账位置、账单行和分录依据；
- `output/index.html`：两个视角的静态仪表盘；
- `output/warehouse.sqlite`：可 SQL 查询的事实投影。

Python API：

```python
from billing_facts import Warehouse

wh = Warehouse()
wh.ingest_jsonl("data/exchanges.jsonl")
report = wh.report()

wh.original_bill("2026-10")       # 封账时快照，不被后续重跑替换
wh.pending_ranges("m7")           # 统一时间上的待查/冲突
wh.unclaimed_ranges()             # 有效使用但无租约/未分完份额
wh.explain_line("LINE-...")       # 账单行覆盖的设备时间与计费依据
```

## 事实流

1. **来源版本**：日志/租约按 `id`、对照按 `(meter, boot)` 取较大 `rev` 整条替代；
   同版同内容重复幂等，同版异内容列为来源冲突，不自行选择。
2. **统一时间**：同一 `(meter, boot)` 内用 `zero + tick` 还原 UTC，只分配有效
   `[from_tick, to_tick)` 对照覆盖的片段；覆盖外片段暂挂。
3. **状态合成**：重叠日志去重，优先级为 `fault > cal > use`；`unknown` 或无证据
   缺口在没有更高优先级状态时列为待查。`cal`、`fault`、停机状态单独保存，不按
   开机总时长收费。若后续出现维修/校准确认且明确写明等待时间可收费，证据模块会把
   对应片段标记为 `evidence_confirmed_chargeable_*`。
4. **责任分配**：
   - 无 `group` 且唯一有效租约为专用；
   - 相同非空 `group` 按精确百分数分摊，未分完部分为未认领；
   - 专用租约撞上其他有效租约、不同分摊组相撞或份额合计超过 100 时，区间保持
     `lease_conflict`/`invalid_share_sum`，原始用量不丢、不挑合同。
5. **费率与额度**：按合同服务区间和 UTC 自然月切分；份额、分摊秒和金额全程使用
   `Decimal`。免费额度按 `(发生 UTC 秒, meter, 源号)` 排序消耗，同一秒的多设备
   逐秒按设备号、源号排序。发票行总额只做一次 `ROUND_HALF_UP` 整分舍入。
6. **封账**：阻断性待查区间必须被该月 `allowed_holds` 覆盖；已知未认领余额不虚构
   责任，但会继续保留。封账快照保存当时的有效事实、账单行和应收金额；之后重跑只
   形成新事实和调账，不替换快照。
7. **旧月更正**：关闭月按**旧月费率和旧月额度**重算完整应收，与该合同/费用月的
   `posted` 回执比较，在当前开放账期生成一条带完整依据的 signed 调整分录。依据中
   同时保留“原账 vs 已入账”和“更正后旧账 vs 原账”两个备注分量。后续新事实只生成
   增量差额；回执号幂等，`adjustment_id`/`entry_id`/`origin` 可显式绑定分录，未
   显式绑定时对封账后新回执按顺序核销。始终分别输出 `booked_after_creation_cents`
   与 `unposted_amount_cents`。

## 样例资料的关键结果

第一次封账 `2026-10` 使用当时的 rev1 对照和 b7 rev1 租约：

| 合同 | 分摊用量 | 免费 | 计费秒 | 精确金额 | 原账发票行 |
|---|---:|---:|---:|---:|---:|
| A | 83 | 50 | 33 | 41.250 分 | **41 分** |
| B | 22 | 10 | 12 | 9.000 分 | **9 分** |

随后 rev2 对照把 m7 起点移动到 `2026-10-31T23:59:25Z`，b7 rev2 把专用租约相撞区
间改为 `2026-10-31T23:59:54Z → 2026-11-01T00:00:13Z`（共 19 秒）。其中：

- 10 月部分 6 秒：旧月封账后新出现的冲突，仍待查，不进入旧月应收；
- 11 月部分 13 秒：由 `nov` 封账的 `allowed_holds` 准许暂挂。

按更正后的事实重算旧 10 月：A 合同为 62 分摊秒、50 免费秒、12 计费秒，应收
**15 分**；免费额度的源占用变为 m2 `26.4` 秒、m7 `23.6` 秒。B 仍为 **9 分**。

回执 `erp-631` 已给 A 的 2026-10 费用月入账 `-10` 分，因此在 11 月开放账期生成：

- `J-ADJ-2026-10-A-nov`：`15 - (-10) = ` **+25 分补收**；
  - 备注：原账 41 与已入账 -10 的差为 +51；旧月事实更正为 15 相对原账为 -26；
- `J-ADJ-2026-10-B-nov`：**+9 分补收**。

11 月正常用量封账金额为 A **179 分**（178.5 四舍五入）、B **6 分**。

## SQL 追查示例

```sh
sqlite3 output/warehouse.sqlite \
  "select line_id, for_month, contract, invoice_amount_cents from bill_line;"

sqlite3 output/warehouse.sqlite \
  "select line_id, meter, start_utc, end_utc, allocated_seconds, free_seconds
   from bill_coverage order by line_id, start_utc;"

sqlite3 output/warehouse.sqlite \
  "select meter, start_utc, end_utc, status, contracts_json
   from responsibility where status != 'allocated';"
```

## 资料字典

来源记录使用 UTF-8 JSON Lines，每行有交付次序 `step`、记录类型 `kind` 和载荷
`body`。字段原始说明见 [`data/字典.md`](data/字典.md)。
