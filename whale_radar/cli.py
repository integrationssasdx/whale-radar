"""命令行入口：``whale-radar analyze`` / ``trace`` / ``rank``
从 stdin 读 JSON、向 stdout 写 JSON。

输入错误不落任何部分报告：向 stderr 输出 ``{"error": 错误码}`` 并以退出码 2
结束；成功时退出码 0。全程不联网、不落盘。
"""

import argparse
import json
import sys

from . import __version__
from .analyzer import AnalyzeError, analyze
from .ranker import rank
from .risk import trace_risk
from .tracer import trace


def build_parser():
    parser = argparse.ArgumentParser(
        prog="whale-radar",
        description="链上异常与巨鲸追踪：资金流图、异常打分与告警路由",
    )
    parser.add_argument(
        "--version", action="version", version="whale-radar %s" % __version__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "analyze", help="从 stdin 读取 JSON，分析结果 JSON 写入 stdout"
    )
    subparsers.add_parser(
        "trace", help="从 stdin 读取 JSON，资金路径追踪结果 JSON 写入 stdout"
    )
    subparsers.add_parser(
        "trace-risk",
        help="从 stdin 读取 JSON，风险路径与告警 JSON 写入 stdout",
    )
    subparsers.add_parser(
        "rank", help="从 stdin 读取 JSON，巨鲸画像与聚合告警 JSON 写入 stdout"
    )
    return parser


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
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            return _run(analyze)
        if args.command == "trace":
            return _run(trace)
        if args.command == "trace-risk":
            return _run(trace_risk)
        if args.command == "rank":
            return _run(rank)
    except AnalyzeError as exc:
        json.dump({"error": exc.code}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
