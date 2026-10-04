"""命令行入口：``whale-radar analyze`` / ``trace`` / ``trace-risk`` / ``rank``。

四个子命令均从 stdin 读一个 JSON 值、向 stdout 写一个 JSON 对象：

- 成功：stdout 只写 ``{"data": ...}`` 加一个换行，退出码 0。
- 输入/业务错误（``AnalyzeError``）：stdout 不写任何内容，stderr 写
  ``{"error": 错误码}`` 加一个换行，退出码 2。错误码、触发条件与优先级
  完全由 analyzer/tracer/risk/ranker 决定，本模块不改变它们。
- 命令行本身非法（缺少或未知子命令、未知选项、向不接受选项的子命令
  传入选项、``--version`` 与子命令混用）：stdout 为空，stderr 只写
  ``{"error": "INVALID_COMMAND"}`` 加换行，退出码 2。
- ``--version`` 单独使用时输出 ``whale-radar <版本>`` 并以退出码 0 结束。

仅依赖 Python 3 标准库；不联网、不落盘、不读取凭证、不修改输入文件。
``python -m whale_radar`` 与 ``bin/whale-radar`` 启动器共用本入口，
参数解析、JSON 往返与退出码完全一致。
"""

import json
import sys

from . import __version__
from .analyzer import AnalyzeError, analyze
from .ranker import rank
from .risk import trace_risk
from .tracer import trace

VERSION_LINE = "whale-radar %s\n" % __version__
INVALID_COMMAND_LINE = '{"error": "INVALID_COMMAND"}\n'

_HANDLERS = {
    "analyze": analyze,
    "trace": trace,
    "trace-risk": trace_risk,
    "rank": rank,
}


class CommandError(Exception):
    """命令行参数非法（对应 INVALID_COMMAND）。"""


def parse_args(argv):
    """解析命令行参数，返回子命令名或 ``"__version__"``。

    语法刻意保持极小：``whale-radar --version`` 或
    ``whale-radar <analyze|trace|trace-risk|rank>``；子命令不接受任何
    选项或额外位置参数，``--version`` 也不得与子命令混用。其余一切
    形式（无参数、未知子命令、未知选项等）都抛出 :class:`CommandError`。
    """
    if len(argv) == 1 and argv[0] == "--version":
        return "__version__"
    if len(argv) == 1 and argv[0] in _HANDLERS:
        return argv[0]
    raise CommandError


def _reconfigure_streams():
    """强制 stdin/stdout/stderr 使用 UTF-8，输出换行固定为 LF。

    这样在 Windows 的 cmd/PowerShell（不同代码页、CRLF 翻译）与类 Unix
    shell 下，重定向到管道或文件时得到字节级一致的结果；非 ASCII 内容
    也不依赖区域设置。测试替身（如 ``io.StringIO``）可能不支持
    reconfigure，忽略即可。
    """
    for stream, newline in (
        (sys.stdin, None),
        (sys.stdout, "\n"),
        (sys.stderr, "\n"),
    ):
        try:
            if newline is None:
                stream.reconfigure(encoding="utf-8")
            else:
                stream.reconfigure(encoding="utf-8", newline=newline)
        except (AttributeError, ValueError):
            pass


def _run(handler):
    try:
        raw = sys.stdin.read()
    except UnicodeDecodeError:
        # 非 UTF-8 字节流不可能是约定的 JSON 输入。
        raise AnalyzeError("INPUT_NOT_JSON")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise AnalyzeError("INPUT_NOT_JSON")
    data = handler(payload)
    # 先完整序列化再写：即便序列化失败，stdout 上也不会留下部分报告。
    sys.stdout.write(json.dumps({"data": data}, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


def main(argv=None):
    _reconfigure_streams()
    if argv is None:
        argv = sys.argv[1:]
    try:
        command = parse_args(argv)
    except CommandError:
        sys.stderr.write(INVALID_COMMAND_LINE)
        return 2
    if command == "__version__":
        sys.stdout.write(VERSION_LINE)
        return 0
    try:
        return _run(_HANDLERS[command])
    except AnalyzeError as exc:
        sys.stderr.write(
            json.dumps({"error": exc.code}, ensure_ascii=False) + "\n"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
