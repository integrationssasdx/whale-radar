# Whale Radar

链上异常与巨鲸追踪引擎：资金流图、异常打分、告警路由，以及可疑资金路径追踪。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。仅使用 Python 3
标准库，全程不联网、不落盘。

## 命令

### `whale-radar analyze`

从 stdin 读取 JSON 对象（`transfers`、`whale_threshold_usd`、`routes`），
向 stdout 输出 `{"data": {"graph", "whales", "scores", "alerts"}}`。

### `whale-radar trace`

从 stdin 读取 JSON 对象，沿同链同资产转账追踪资金路径。stdout 只输出路径
追踪结果，不混入 analyze 数据。

输入字段：

- `transfers`：公开转账结构（`id`、`timestamp`、`chain`、`asset`、
  `from_address`、`to_address`、`amount`、`usd_value`）。
- `chain`、`asset`：只在二者完全匹配的转账中沿 from_address 到
  to_address 查找。
- `start_address`、`end_address`：路径起点与终点（非空且不同）。
- `max_hops`：1 到 8 的整数，路径跳数不少于 1 且不超过它。

成功输出 `{"data": {"paths": [...]}}`，无路径时为空数组且仍成功。每个路径：

- `nodes`：起点、中间地址、终点，按顺序排列，路径内地址不重复（简单路径）。
- `transfer_ids`：路径上各跳对应的转账 id。
- `hops`：跳数。
- `amount`、`usd_value`：路径转账值总和，保留十位小数，整数不带小数部分。

多条路径按 `hops` 升序；`hops` 相同按 `transfer_ids`、`nodes` 字典序。

## 错误约定

成功退出码 0；输入错误退出码 2，stderr 仅输出 `{"error": 错误码}`，stdout
不写部分结果。trace 的校验顺序为：

INPUT_NOT_JSON → INVALID_INPUT_SCHEMA → DUPLICATE_TRANSFER_ID
→ INVALID_TRANSFER_VALUE → INVALID_TRACE_QUERY

后续错误不覆盖先前错误：时间戳无时区或不可解析、`amount` 非有限正数、
`usd_value` 非有限非负数均为 `INVALID_TRANSFER_VALUE`；查询字段缺失或类型
错误、地址为空或相同、`max_hops` 不是 1 到 8 的整数统一为
`INVALID_TRACE_QUERY`。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
