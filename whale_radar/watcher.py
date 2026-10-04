"""关注地址协同资金流观察：``whale-radar watch`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 watch_addresses、max_hops），输出 data 仅含
paths、alerts 两个数组。

paths 枚举同链同资产上、从一个关注地址出发到另一个关注地址结束的有向
简单路径（跳数 1..max_hops）。同一对端点间不同转账序列（含并行转账）
各为一条路径，与 trace 的枚举口径一致。逐段分值与原因沿用 analyze 的
逐笔打分，路径分值为各段之和（截断到 0..100），原因按 VALUE BURST
FAN_OUT ROUND_TRIP 顺序去重。

校验复用 analyzer 与 tracer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_WATCH_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

from .analyzer import (
    AnalyzeError,
    _match,
    _round10,
    _score_transfers,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)
from .tracer import MAX_HOPS_MAX, MAX_HOPS_MIN, _find_paths

WATCH_FIELDS = ("watch_addresses", "max_hops")

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _validate_watch_query(payload):
    """关注查询校验，返回 (watch_addresses, max_hops)。

    watch_addresses 至少含两个互异非空字符串；max_hops 为 1..8 整数
    （不接受 bool）。
    """
    for field in WATCH_FIELDS:
        if field not in payload:
            raise AnalyzeError("INVALID_WATCH_QUERY")
    watches = payload["watch_addresses"]
    max_hops = payload["max_hops"]
    if not isinstance(watches, list) or not watches:
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if not all(isinstance(item, str) and item for item in watches):
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if len(set(watches)) < 2:
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if not (MAX_HOPS_MIN <= max_hops <= MAX_HOPS_MAX):
        raise AnalyzeError("INVALID_WATCH_QUERY")
    return watches, max_hops


def _find_watch_paths(transfers, watches, max_hops):
    """枚举关注地址两两之间同链同资产的简单路径。

    对每个 (chain, asset) 与每个有向关注地址对 (source, target) 复用
    tracer 的简单路径搜索；同一链同一资产内的全部简单路径（1..max_hops
    跳）都会保留，包括经过第三个关注地址的路径。
    """
    groups = sorted({(t["chain"], t["asset"]) for t in transfers})
    endpoints = sorted(set(watches))

    paths = []
    for chain, asset in groups:
        group_transfers = [
            t for t in transfers
            if t["chain"] == chain and t["asset"] == asset
        ]
        for source in endpoints:
            for target in endpoints:
                if source == target:
                    continue
                for path in _find_paths(
                    group_transfers, chain, asset, source, target, max_hops
                ):
                    paths.append(
                        {
                            "nodes": path["nodes"],
                            "transfer_ids": path["transfer_ids"],
                            "hops": path["hops"],
                            "amount": path["amount"],
                            "usd_value": path["usd_value"],
                            "from_address": source,
                            "to_address": target,
                            "chain": chain,
                            "asset": asset,
                            "path_id": ">".join(path["transfer_ids"]),
                        }
                    )
    return paths


def _watch_paths(transfers, threshold, config, watches, max_hops):
    """在拓扑路径上附加逐段合并的 analyze 分值与原因。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    raw_paths = _find_watch_paths(transfers, watches, max_hops)

    paths = []
    for path in raw_paths:
        segment_scores = [scores_by_id[tid] for tid in path["transfer_ids"]]
        total = sum(item["score"] for item in segment_scores)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [reason for reason in REASON_ORDER if reason in merged]

        paths.append(
            {
                "nodes": path["nodes"],
                "transfer_ids": path["transfer_ids"],
                "hops": path["hops"],
                "amount": path["amount"],
                "usd_value": path["usd_value"],
                "from_address": path["from_address"],
                "to_address": path["to_address"],
                "chain": path["chain"],
                "asset": path["asset"],
                "path_id": path["path_id"],
                "score": _round10(max(0.0, min(100.0, total))),
                "reason": reasons,
            }
        )

    paths.sort(
        key=lambda item: (
            -item["score"],
            item["hops"],
            item["chain"],
            item["asset"],
            item["transfer_ids"],
        )
    )
    return paths


def _watch_alerts(paths, routes):
    """每条 route 与每条 path 至多一项告警，按 route_id、path_id 排序。"""
    alerts = []
    for route in routes:
        for path in paths:
            if path["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], path["chain"]):
                continue
            if not _match(route["assets"], path["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "path_id": path["path_id"],
                    "from_address": path["from_address"],
                    "to_address": path["to_address"],
                    "chain": path["chain"],
                    "asset": path["asset"],
                    "severity": route["severity"],
                    "score": path["score"],
                    "reason": list(path["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["path_id"]))
    return alerts


def watch(payload):
    """对已解析的输入 JSON 执行关注地址协同资金流观察，返回 data。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    watches, max_hops = _validate_watch_query(payload)
    config = _validate_scoring(payload)

    paths = _watch_paths(transfers, threshold, config, watches, max_hops)
    alerts = _watch_alerts(paths, routes)
    return {"paths": paths, "alerts": alerts}
