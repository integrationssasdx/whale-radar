"""命令行入口：``whale-radar analyze`` / ``trace`` / ``trace-risk`` /
``rank`` / ``watch`` 从 stdin 读 JSON、向 stdout 写 JSON。

输入错误不落任何部分报告：向 stderr 输出 ``{"error": 错误码}`` 并以退出码 2
结束；成功时退出码 0。全程不联网、不落盘。

命令行用法错误（缺少子命令、未知子命令、未知选项、向子命令传入选项、
``--version`` 与子命令混用）统一输出 ``{"error": "INVALID_COMMAND"}`` 到
stderr，stdout 为空，退出码 2。参数解析手工进行，不依赖 argparse 的
usage 文本，以保证 stderr 只含错误码 JSON。
"""

import json
import sys

from . import __version__
from .analyzer import AnalyzeError, analyze
from .ranker import rank
from .risk import trace_risk
from .tracer import trace
from .watcher import watch

_COMMANDS = {
    "analyze": analyze,
    "trace": trace,
    "trace-risk": trace_risk,
    "rank": rank,
    "watch": watch,
}

VERSION_LINE = "whale-radar %s" % __version__


def _write_error(code):
    json.dump({"error": code}, sys.stderr, ensure_ascii=False)
    sys.stderr.write("\n")
    return 2


def _run(handler):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise AnalyzeError("INPUT_NOT_JSON")
    data = handler(payload)
    json.dump({"data": data}, sys.stdout, ensure_ascii=False, sort_keys=False)
    sys.stdout.write("\n")
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    # --version 仅可单独出现；与子命令或其他参数混用属于 INVALID_COMMAND。
    if argv == ["--version"]:
        sys.stdout.write(VERSION_LINE + "\n")
        return 0
    if len(argv) == 1 and argv[0] in _COMMANDS:
        try:
            return _run(_COMMANDS[argv[0]])
        except AnalyzeError as exc:
            return _write_error(exc.code)
    return _write_error("INVALID_COMMAND")


if __name__ == "__main__":
    sys.exit(main())
