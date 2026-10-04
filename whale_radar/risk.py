"""风险路径追踪：``whale-radar trace-risk`` 的核心逻辑。

在 trace 的同链同资产简单路径搜索之上，逐段沿用 analyze 的异常分值与原因：

- amount / usd_value 沿路径分段求和（口径同 trace，round10）；
- score 为各段 analyze 分值之和，上限 100（round10）；
- reason 合并各段原因后按 VALUE / BURST / FAN_OUT / ROUND_TRIP 顺序去重；
- route 命中路径生成告警，score / reason 取路径值，同 route/path 至多一项。

校验顺序与错误码优先级：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_TRACE_QUERY
"""

from __future__ import annotations

from .analyzer import (
    REASON_BURST,
    REASON_FAN_OUT,
    REASON_ROUND_TRIP,
    REASON_VALUE,
    _match,
    _round10,
    _score_transfers,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_threshold,
    _validate_values,
)
from .tracer import _find_paths, _validate_query

# 路径原因的固定输出顺序。
REASON_ORDER = (REASON_VALUE, REASON_BURST, REASON_FAN_OUT, REASON_ROUND_TRIP)


def _merge_reasons(transfer_ids, scores_by_id):
    """合并各段原因并按固定顺序去重。"""
    merged = []
    for reason in REASON_ORDER:
        if any(reason in scores_by_id[tid]["reason"] for tid in transfer_ids):
            merged.append(reason)
    return merged


def _build_paths(transfers, threshold, chain, asset, start, end, max_hops):
    """路径搜索 + 逐段 analyze 分值汇总。"""
    scores = _score_transfers(transfers, threshold)
    scores_by_id = {item["id"]: item for item in scores}

    paths = []
    for raw in _find_paths(
        transfers, chain, asset, start, end, max_hops
    ):
        transfer_ids = raw["transfer_ids"]
        total_score = sum(
            scores_by_id[tid]["score"] for tid in transfer_ids
        )
        paths.append(
            {
                "nodes": raw["nodes"],
                "transfer_ids": transfer_ids,
                "hops": raw["hops"],
                "amount": raw["amount"],
                "usd_value": raw["usd_value"],
                "score": _round10(min(100.0, total_score)),
                "reason": _merge_reasons(transfer_ids, scores_by_id),
                "path_id": ">".join(transfer_ids),
            }
        )

    paths.sort(key=lambda path: (-path["score"], path["hops"],
                                 path["transfer_ids"]))
    return paths


def _trace_risk_alerts(paths, routes, chain, asset):
    """route 命中路径生成告警；路径均在查询的 chain/asset 上。"""
    alerts = []
    for route in routes:
        for path in paths:
            if path["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], chain):
                continue
            if not _match(route["assets"], asset):
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

    paths = _build_paths(
        transfers, threshold, chain, asset, start, end, max_hops
    )
    alerts = _trace_risk_alerts(paths, routes, chain, asset)
    return {"paths": paths, "alerts": alerts}
