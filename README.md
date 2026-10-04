# Whale Radar

链上异常与巨鲸追踪引擎：资金流图、异常打分与告警路由。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

基线已含 analyze、trace、trace-risk、rank 四个子命令的分析实现，以及
仓库根目录的产品入口与 `python -m whale_radar` 模块入口。

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

四个子命令均从 stdin 读取一个 JSON 值、成功时向 stdout 写一个含 `data`
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
