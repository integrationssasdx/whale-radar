"""风险路径追踪与告警：``whale-radar trace-risk`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 trace 的 chain、asset、start_address、end_address、
max_hops），输出 data 仅含 paths、alerts 两个数组。

路径搜索沿用 tracer 的同链同资产简单路径；逐段分值与原因沿用 analyze 的
逐笔打分，路径分值为各段之和（上限 100），原因按
VALUE BURST FAN_OUT ROUND_TRIP 顺序去重。

校验复用 analyzer 与 tracer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_TRACE_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

from .analyzer import (
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
from .tracer import _find_paths, _validate_query

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _risk_paths(transfers, threshold, config, chain, asset, start, end,
                max_hops):
    """在 trace 拓扑路径上附加逐段合并的 analyze 分值、原因与 path_id。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    paths = _find_paths(transfers, chain, asset, start, end, max_hops)

    risk_paths = []
    for path in paths:
        segment_scores = [scores_by_id[tid] for tid in path["transfer_ids"]]
        total = sum(item["score"] for item in segment_scores)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [reason for reason in REASON_ORDER if reason in merged]

        risk_paths.append(
            {
                "nodes": path["nodes"],
                "transfer_ids": path["transfer_ids"],
                "hops": path["hops"],
                "amount": path["amount"],
                "usd_value": path["usd_value"],
                "score": _round10(min(100.0, total)),
                "reason": reasons,
                "path_id": ">".join(path["transfer_ids"]),
            }
        )

    risk_paths.sort(
        key=lambda item: (-item["score"], item["hops"], item["transfer_ids"])
    )
    return risk_paths


def _risk_alerts(paths, routes, chain, asset):
    """每条 route 与每条 path 至多一项告警，按 route_id、path_id 排序。"""
    alerts = []
    for route in routes:
        if not _match(route["chains"], chain):
            continue
        if not _match(route["assets"], asset):
            continue
        for path in paths:
            if path["score"] < route["min_score"]:
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "path_id": path["path_id"],
                    "severity": route["severity"],
                    "score": path["score"],
                    "reason": list(path["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["path_id"]))
    return alerts


def trace_risk(payload):
    """对已解析的输入 JSON 执行风险路径追踪，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    chain, asset, start, end, max_hops = _validate_query(payload)
    config = _validate_scoring(payload)

    paths = _risk_paths(
        transfers, threshold, config, chain, asset, start, end, max_hops
    )
    alerts = _risk_alerts(paths, routes, chain, asset)
    return {"paths": paths, "alerts": alerts}
