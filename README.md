# Whale Radar

链上异常与巨鲸追踪引擎：资金流图、异常打分与告警路由。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

## 状态

核心分析能力（analyze / trace / trace-risk / rank）与命令行入口均已实现。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。

## 入口

在仓库根目录下，以下两种入口等价，参数解析、JSON 往返、非 ASCII 字符、
流重定向与退出码完全一致；均只依赖 Python 3 标准库，不依赖 PYTHONPATH、
pip 安装或联网：

- `bin/whale-radar`：类 Unix shell 启动器（按自身位置定位同仓库内的
  `whale_radar` 包，从任意当前目录、仓库路径含空格均可运行）。
- `bin/whale-radar.cmd`：Windows cmd / PowerShell 启动器（同上）。
- `python -m whale_radar`：模块入口（从仓库根目录运行）。

`--version` 单独使用时输出 `whale-radar 0.1.0` 并以退出码 0 结束。

## 命令

均从 stdin 读取一个 JSON 值、向 stdout 写一个 JSON 对象；成功时 stdout
只写 `{"data": ...}` 加换行，退出码 0。输入错误以退出码 2 结束，stdout
不写任何内容（不留部分报告），stderr 只写 `{"error": 错误码}` 加换行。
错误码、触发条件与优先级为：

INPUT_NOT_JSON → INVALID_INPUT_SCHEMA → DUPLICATE_TRANSFER_ID
→ INVALID_TRANSFER_VALUE → INVALID_THRESHOLD → INVALID_ROUTE
→ INVALID_TRACE_QUERY（trace / trace-risk 专用）

命令行本身非法——缺少子命令、未知子命令、未知选项、向不接受选项的
子命令传入选项，或把 `--version` 与子命令混用——时同样以退出码 2
结束，stdout 为空，stderr 只写 `{"error": "INVALID_COMMAND"}` 加换行。

- `bin/whale-radar analyze`：资金流图、巨鲸转账、逐笔异常打分与告警路由。
- `bin/whale-radar trace`：同链同资产上的资金路径追踪。
- `bin/whale-radar trace-risk`：在 trace 拓扑路径上附加 analyze 逐段分值与
  原因的风险路径（paths）及按 route/path 聚合的告警（alerts）。输入为 analyze
  的 transfers、whale_threshold_usd、routes 加 trace 的 chain、asset、
  start_address、end_address、max_hops，成功时 data 仅含 `paths`、`alerts`。
- `bin/whale-radar rank`：高风险巨鲸地址画像（profiles）与按 route/地址
  聚合的告警（alerts）。输入同 analyze（transfers、whale_threshold_usd、
  routes），成功时 data 仅含 `profiles`、`alerts` 两个数组。
