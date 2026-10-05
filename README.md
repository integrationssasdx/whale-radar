# Whale Radar

链上异常与巨鲸追踪引擎：资金流图、异常打分与告警路由。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

基线已含 analyze、trace、trace-risk、rank、watch、converge 六个子命令的
分析实现；在此之上新增 cycles 子命令，发现同链同资产转账的多跳回流
回路。各命令均提供仓库根目录的产品入口与 `python -m whale_radar` 模块
入口。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

## 入口

以下三种方式行为完全一致（参数解析、JSON 往返、非 ASCII、流重定向、
退出码与异常顺序），均从仓库根目录可用，仓库路径含空格亦不受影响；
启动器自行定位同仓库的 `whale_radar` 包，不依赖 PYTHONPATH、pip 安装、
联网或运行期落盘：

- `./whale-radar`：类 Unix shell 直接执行（Windows 下用 `whale-radar.cmd`，
  cmd 与 PowerShell 均可）。
- `python -m whale_radar`：模块入口。
- `bin/whale-radar`：与根目录入口等价的备用启动器。

`whale-radar --version` 输出 `whale-radar 0.1.0` 并以退出码 0 结束。
缺少子命令、未知子命令、未知选项、向子命令传入选项，或把 `--version`
与子命令混用，统一向 stderr 输出 `{"error": "INVALID_COMMAND"}` 并换行，
stdout 为空，退出码 2。

## 命令

各子命令均从 stdin 读取一个 JSON 值、成功时向 stdout 写一个含 `data`
的 JSON 对象和换行（退出码 0）；输入错误不落任何部分报告，以退出码 2
结束并向 stderr 输出 `{"error": 错误码}` 和换行。仅依赖 Python 3 标准库：

- `whale-radar analyze`：资金流图、巨鲸转账、逐笔异常打分与告警路由。
- `whale-radar trace`：同链同资产上的资金路径追踪。
- `whale-radar trace-risk`：在 trace 拓扑路径上附加 analyze 逐段分值与
  原因的风险路径（paths）及按 route/path 聚合的告警（alerts）。输入为 analyze
  的 transfers、whale_threshold_usd、routes 加 trace 的 chain、asset、
  start_address、end_address、max_hops，成功时 data 仅含 `paths`、`alerts`。
- `whale-radar rank`：高风险巨鲸地址画像（profiles）与按 route/地址
  聚合的告警（alerts）。输入同 analyze（transfers、whale_threshold_usd、
  routes），成功时 data 仅含 `profiles`、`alerts` 两个数组。
- `whale-radar watch`：关注地址间的协同资金流（paths）与按 route/path
  聚合的告警（alerts）。输入沿用 analyze 的 transfers、whale_threshold_usd、
  routes 与可选 scoring，外加至少两个互异非空字符串的 `watch_addresses`
  和 1..8 整数 `max_hops`。paths 枚举同链同资产上关注地址之间的有向简单
  路径（地址不重复，跳数 1..max_hops，路径两端均为关注地址），每项含
  nodes、transfer_ids、hops、amount、usd_value、from_address、to_address、
  chain、asset、path_id、score、reason；金额按 10 位小数汇总，path_id 由
  transfer_ids 以 `>` 连接。score 为 analyze 逐段分值求和后截到 0..100，
  reason 按 VALUE、BURST、FAN_OUT、ROUND_TRIP 去重；paths 按 score 降序，
  再按 hops、chain、asset、transfer_ids 升序。alerts 每个 route/path 至多
  一项，score≥route.min_score 且 chains、assets 命中星号规则时生成，每项含
  route_id、path_id、from_address、to_address、chain、asset、severity、
  score、reason、target，按 route_id、path_id 排序。无路径时 paths、alerts
  均为空数组。watch 的错误码顺序为 INPUT_NOT_JSON > INVALID_INPUT_SCHEMA
  > DUPLICATE_TRANSFER_ID > INVALID_TRANSFER_VALUE > INVALID_THRESHOLD
  > INVALID_ROUTE > INVALID_WATCH_QUERY > INVALID_SCORING_CONFIG。
- `whale-radar converge`：多来源短时汇入同一接收方的归集事件（events）与
  按 route/event 聚合的告警（alerts）。输入沿用 analyze 的 transfers、
  whale_threshold_usd、routes 与可选 scoring，外加 `convergence` 对象：
  `window_seconds`（1..10000 整数）、`min_sources`（2..10000 整数）、
  `min_usd_value`（非负有限数），三者缺一不可、不接受未知字段与布尔值。
  转账按 (chain, asset, to_address) 分组并去除自转账，组内按 timestamp、
  id 升序；以每笔到账为起点，纳入 timestamp≤起点+window_seconds 的到账
  构成窗口，窗口内不同来源数≥min_sources 且 usd_value 合计≥min_usd_value
  时输出事件，每项含 event_id、recipient、chain、asset、source_count、
  transfer_ids、amount、usd_value、score、reason；transfer_ids 按组内顺序
  排列并以 `>` 连接为 event_id，金额与 score 保留 10 位小数。score=
  min(100, 20*(source_count-min_sources+1)+50*usd_value/whale_threshold_usd)；
  reason 以 FAN_IN 起，usd_value≥whale_threshold_usd 时追加 VALUE。
  events 按 score 降序、event_id 升序排序，空时保留空数组。alerts 每个
  route/event 至多一项，score≥route.min_score 且 chains、assets 命中
  星号规则时生成，字段沿用 watch 告警（path_id、from_address 改为
  event_id、recipient），按 route_id、event_id 排序。converge 的错误码
  顺序为 INPUT_NOT_JSON > INVALID_INPUT_SCHEMA > DUPLICATE_TRANSFER_ID
  > INVALID_TRANSFER_VALUE > INVALID_THRESHOLD > INVALID_ROUTE
  > INVALID_CONVERGENCE_QUERY > INVALID_SCORING_CONFIG。
- `whale-radar cycles`：同链同资产转账的多跳回流回路（cycles）与按
  route/cycle 聚合的告警（alerts）。输入沿用 analyze 的 transfers、
  whale_threshold_usd、routes 与可选 scoring，外加 `cycle_query` 对象：
  `max_hops`（2..8 整数）与 `min_usd_value`（非负有限数），二者缺一不可、
  不接受未知字段与布尔值。回路为同链同资产上 2..max_hops 跳的地址简单
  回路（仅末节点重复起点），不同边序列（含同一回路的反向边序列）均保留；
  nodes 从字典序最小的参与地址起并回到它，每项含 transfer_ids、cycle_id
  （transfer_ids 以 `>` 连接）及 hops、chain、asset、amount、usd_value、
  score、reason；金额与分值保留 10 位小数，过滤 usd_value 小于
  min_usd_value 的回路。score 为 analyze 逐段分值求和后截到 0..100，
  reason 按 VALUE、BURST、FAN_OUT、ROUND_TRIP 顺序去重；cycles 按 score
  降序，再按 chain、asset、cycle_id 升序排序，空时保留空数组。alerts
  每个 route/cycle 至多一项，score≥route.min_score 且 chains、assets
  命中星号规则时生成，每项含 route_id、cycle_id、chain、asset、severity、
  score、reason、target，按 route_id、cycle_id 排序。cycles 的错误码
  顺序为 INPUT_NOT_JSON > INVALID_INPUT_SCHEMA > DUPLICATE_TRANSFER_ID
  > INVALID_TRANSFER_VALUE > INVALID_THRESHOLD > INVALID_ROUTE
  > INVALID_CYCLE_QUERY > INVALID_SCORING_CONFIG。

## scoring 配置

`analyze`、`trace-risk`、`rank`、`watch`、`cycles` 接受可选的顶层
`scoring` 对象，逐项覆盖异常打分的窗口、阈值与分值；省略 `scoring`
或仅提供部分字段时，缺失项沿用下列基线值，输出与基线完全一致。
`converge` 校验 `scoring`（非法时报 INVALID_SCORING_CONFIG）但不将其
用于归集打分；`trace` 不做打分，`scoring`（即使字段未知或非法）对其
完全忽略。

| 字段 | 基线 | 类型与范围 |
| --- | --- | --- |
| `window_seconds` | 3600 | 1..10000 整数 |
| `burst_count` | 5 | 1..10000 整数 |
| `fan_out_recipients` | 3 | 1..10000 整数 |
| `value_points_per_ratio` | 20 | 0..1000 有限数 |
| `value_points_cap` | 40 | 0..100 有限数 |
| `transfer_burst_points` | 25 | 0..100 有限数 |
| `fan_out_points` | 20 | 0..100 有限数 |
| `round_trip_points` | 15 | 0..100 有限数 |
| `whale_points` | 40 | 0..100 有限数 |
| `counterparty_count` | 3 | 1..10000 整数 |
| `counterparty_points` | 25 | 0..100 有限数 |
| `address_round_trip_points` | 20 | 0..100 有限数 |
| `address_burst_points` | 15 | 0..100 有限数 |

整数字段不接受浮点、字符串或布尔值；数值字段不接受布尔值、`NaN`、
`Infinity`。逐笔基础分为 `usd_value / whale_threshold_usd`
× `value_points_per_ratio`，截到 `value_points_cap`；突发、扇出、
往返命中再各加对应分值，总分限制在 0..100，窗口为闭区间。地址画像
沿用原有原因（WHALE_EXPOSURE、COUNTERPARTY_DISTRIBUTION、
ROUND_TRIP_ACTIVITY、BURST_ACTIVITY）与地址分值，总分限制在 0..100。
trace-risk 的路径分值仍为各段之和后限制在 100，只返回 `paths` 与
`alerts`。原因名称与顺序、阈值与 `min_score` 的等值边界、星号匹配、
告警去重与稳定排序均不随配置改变。

`scoring` 不是对象、含未知字段，或任一字段类型/范围不合法时，
analyze、trace-risk、rank、watch、cycles、converge 六个命令均向 stderr
输出 `{"error": "INVALID_SCORING_CONFIG"}` 和换行，stdout 为空，退出码
2，且不落任何部分报告。该错误在既有校验全部通过之后才触发：analyze 与
rank 中晚于 INVALID_ROUTE，trace-risk 中晚于 INVALID_TRACE_QUERY，
watch 中晚于 INVALID_WATCH_QUERY，cycles 中晚于
INVALID_CYCLE_QUERY，converge 中晚于 INVALID_CONVERGENCE_QUERY；
`trace` 永不产生此错误。
