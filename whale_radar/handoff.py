"""跨链 handoff 追踪：``whale-radar handoff`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 handoff_query 查询对象），输出 data 仅含
handoffs、alerts 两个数组；无跨链交接时两者均为空数组。

handoff_query 必须恰好含 window_seconds（1..86400 整数，不接受 bool）与
min_usd_value（非负有限数，不接受 bool）；缺失、含未知字段或非法值均报
INVALID_HANDOFF_QUERY。

handoff 为一对有序转账（首 -> 次）构成的跨链交接：两笔 id 不同且均非自
转账，asset 相同，首笔 to_address 等于次笔 from_address（交接地址
intermediary），chain 不同，次笔时间减首笔时间落在 [0, window_seconds]
闭区间，且事件 usd_value（两笔美元价值的较大者）不低于 min_usd_value。
时间相同的两笔按两种顺序各自成事件。

每项含 event_id（transfer_ids 以 ``>`` 连接）、transfer_ids（首、次顺序）、
intermediary、source_/destination_ 前缀的 address、chain、amount、
usd_value（首/次笔的发送侧与接收侧）、amount_delta 与 usd_delta（次减
首）、usd_value（两笔较大者），金额类派生值保留 10 位小数。score 为
analyze 逐段分值之和加 15，美元价值下降（次笔小于首笔）再加 10，截到
0..100；reason 以 CROSS_CHAIN_HANDOFF 起，按 VALUE、BURST、FAN_OUT、
ROUND_TRIP 顺序去重合并两段原因，下降时追加 VALUE_DROP。每项另附
segments：与 transfer_ids 同序对应，每项恰含 transfer_id、score
（analyze 同一 scoring 配置下的逐笔 0..100 分，保留 10 位小数）、
reason（按 VALUE、BURST、FAN_OUT、ROUND_TRIP 去重）。handoffs 按
score 降序、event_id 升序排序。alerts 每个 route/event 至多一项，
score>=route.min_score 且 chains 同时命中两端链、assets 命中资产（星号
规则）时生成，字段为 route_id、event_id、intermediary、severity、
score、reason、target，按 route_id、event_id 排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_HANDOFF_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math

from .analyzer import (
    AnalyzeError,
    _is_number,
    _match,
    _round10,
    _score_transfers,
    _segments,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)

HANDOFF_QUERY_FIELDS = ("window_seconds", "min_usd_value")

WINDOW_SECONDS_MIN, WINDOW_SECONDS_MAX = 1, 86400

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")

REASON_HANDOFF = "CROSS_CHAIN_HANDOFF"
REASON_VALUE_DROP = "VALUE_DROP"

HANDOFF_BASE_POINTS = 15.0
VALUE_DROP_POINTS = 10.0


def _validate_handoff_query(payload):
    """handoff_query 校验，返回 (window_seconds, min_usd_value)。

    必须恰好含两个已知字段：window_seconds 为 1..86400 整数（不接受
    bool）、min_usd_value 为非负有限数（不接受 bool）。任何缺失、未知
    字段或非法值均抛 INVALID_HANDOFF_QUERY。
    """
    if "handoff_query" not in payload:
        raise AnalyzeError("INVALID_HANDOFF_QUERY")
    raw = payload["handoff_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_HANDOFF_QUERY")
    if set(raw) != set(HANDOFF_QUERY_FIELDS):
        raise AnalyzeError("INVALID_HANDOFF_QUERY")

    window_seconds = raw["window_seconds"]
    min_usd_value = raw["min_usd_value"]

    if (
        not isinstance(window_seconds, int)
        or isinstance(window_seconds, bool)
        or not (WINDOW_SECONDS_MIN <= window_seconds <= WINDOW_SECONDS_MAX)
    ):
        raise AnalyzeError("INVALID_HANDOFF_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_HANDOFF_QUERY")

    return window_seconds, float(min_usd_value)


def _find_handoffs(transfers, window_seconds):
    """枚举全部满足条件的跨链交接有序对（首 -> 次）。

    按 (asset, 交接地址) 索引首笔候选，次笔只查同资产且 from_address 等于
    首笔 to_address 的转账；时间差闭区间 [0, window_seconds]、链不同、
    双方均非自转账。时间相同的有序对两种顺序各自保留。
    """
    candidates = [
        transfer
        for transfer in transfers
        if transfer["from_address"] != transfer["to_address"]
    ]
    by_asset_to = {}
    for transfer in candidates:
        key = (transfer["asset"], transfer["to_address"])
        by_asset_to.setdefault(key, []).append(transfer)

    pairs = []
    for second in candidates:
        key = (second["asset"], second["from_address"])
        for first in by_asset_to.get(key, ()):
            if first["id"] == second["id"]:
                continue
            if first["chain"] == second["chain"]:
                continue
            delta = (second["timestamp"] - first["timestamp"]).total_seconds()
            if not (0 <= delta <= window_seconds):
                continue
            pairs.append((first, second))
    return pairs


def _build_handoffs(transfers, threshold, config, window_seconds,
                    min_usd_value):
    """枚举跨链交接并附加 analyze 逐段合并的分值、原因与 event_id。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    pairs = _find_handoffs(transfers, window_seconds)

    handoffs = []
    for first, second in pairs:
        usd_value = max(float(first["usd_value"]), float(second["usd_value"]))
        if usd_value < min_usd_value:
            continue

        segment_scores = [scores_by_id[first["id"]], scores_by_id[second["id"]]]
        value_drop = float(second["usd_value"]) < float(first["usd_value"])
        total = sum(item["score"] for item in segment_scores)
        total += HANDOFF_BASE_POINTS + (VALUE_DROP_POINTS if value_drop else 0.0)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [REASON_HANDOFF]
        reasons.extend(reason for reason in REASON_ORDER if reason in merged)
        if value_drop:
            reasons.append(REASON_VALUE_DROP)

        handoffs.append(
            {
                "event_id": "%s>%s" % (first["id"], second["id"]),
                "transfer_ids": [first["id"], second["id"]],
                "intermediary": first["to_address"],
                "source_address": first["from_address"],
                "source_chain": first["chain"],
                "source_amount": first["amount"],
                "source_usd_value": first["usd_value"],
                "destination_address": second["to_address"],
                "destination_chain": second["chain"],
                "destination_amount": second["amount"],
                "destination_usd_value": second["usd_value"],
                "amount_delta": _round10(
                    float(second["amount"]) - float(first["amount"])
                ),
                "usd_delta": _round10(
                    float(second["usd_value"]) - float(first["usd_value"])
                ),
                "usd_value": _round10(usd_value),
                "score": _round10(max(0.0, min(100.0, total))),
                "reason": reasons,
                "segments": _segments(segment_scores),
                # 仅用于告警的资产匹配，不属于输出字段。
                "_asset": first["asset"],
            }
        )

    handoffs.sort(key=lambda item: (-item["score"], item["event_id"]))
    return handoffs


def _handoff_alerts(handoffs, routes):
    """每条 route 与每条 event 至多一项告警，按 route_id、event_id 排序。"""
    alerts = []
    for route in routes:
        for handoff in handoffs:
            if handoff["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], handoff["source_chain"]):
                continue
            if not _match(route["chains"], handoff["destination_chain"]):
                continue
            if not _match(route["assets"], handoff["_asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "event_id": handoff["event_id"],
                    "intermediary": handoff["intermediary"],
                    "severity": route["severity"],
                    "score": handoff["score"],
                    "reason": list(handoff["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["event_id"]))
    return alerts


def handoff(payload):
    """对已解析的输入 JSON 执行跨链交接追踪，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    window_seconds, min_usd_value = _validate_handoff_query(payload)
    config = _validate_scoring(payload)

    handoffs = _build_handoffs(
        transfers, threshold, config, window_seconds, min_usd_value
    )
    alerts = _handoff_alerts(handoffs, routes)
    for item in handoffs:
        del item["_asset"]
    return {"handoffs": handoffs, "alerts": alerts}
