# 共享仪器的计费事实和封账更正

共享仪器的计量与财务交接素材，时间一律采用 UTC。费用、设备号和课题组均为合成资料。

## 资料

来源记录使用 UTF-8 JSON Lines，每行有交付次序 `step`、记录类型 `kind` 和载荷 `body`。按交付次序阅读 `data/exchanges.jsonl`，字段说明见 `data/字典.md`。

## 校验原件

使用 Python 3.11。在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```
