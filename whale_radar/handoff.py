"""跨链交接（handoff）追踪：``whale-radar handoff`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes 与可选 scoring，外加 handoff_query 查询对象），输出 data 仅含
handoffs、alerts 两个数组；无交接时两者均为空数组。

handoff_query 必须恰好含 window_seconds（1..86400 整数，不接受 bool）与
min_usd_value（非负有限数，不接受 bool）；缺失、含未知字段、布尔或类型
范围错误均报 INVALID_HANDOFF_QUERY。

一笔交接由两笔非自转账构成：id 互异、资产相同、链不同（跨链）、首笔（S）
的 to 等于次笔（D）的 from（共同的中间地址 intermediary，资金经其中转）、
时间差在 [0, window_seconds] 闭区间、次笔 usd_value 不低于 min_usd_value。
同一无序笔对的两个时间方向各检查一次（两序），各自满足即各成一个事件。

每项含 source_/destination_ × (address,chain,amount,usd_value)、intermediary、
asset、transfer_ids、event_id、amount_delta、usd_delta（均为次-首）、
usd_value（两端之 max）、score、reason、segments；transfer_ids 与 segments
按 (timestamp, id) 排序，event_id 以 ``>`` 连接，金额与 score 保留 10 位
小数。eventscore = s1 + s2 + 15（ROUND_TRIP 基分）+ 10（次笔美元低于首笔
时的 VALUE_DROP），截到 0..100；reason 以 CROSS_CHAIN_HANDOFF 起，再合并
analyze 的 VALUE、BURST、FAN_OUT、ROUND_TRIP（按此序去重），下降时追加
VALUE_DROP。handoffs 按 score 降序、event_id 升序排序。

alerts 每个 route/event 至多一项，score>=min_score 且 route.chains 同时命中
source_chain 与 destination_chain 两端、route.assets 命中交接资产（均支持
星号）时生成；字段沿用 trace-risk 告警（route_id、severity、score、reason、
target），path_id 改为 event_id 并增列 intermediary，按 route_id、event_id
升序排序。

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

# analyze 四因合并进 reason 时的固定顺序。
REASON_BASE_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")
DROP_REASON = "VALUE_DROP"
HANDOFF_REASON = "CROSS_CHAIN_HANDOFF"

ROUND_TRIP_BASE_POINTS = 15.0
VALUE_DROP_POINTS = 10.0


def _validate_handoff_query(payload):
    """handoff_query 校验，返回 (window_seconds, min_usd_value)。

    必须恰好含两个已知字段：window_seconds 为 1..86400 整数（不接受 bool）、
    min_usd_value 为非负有限数（不接受 bool）。任何缺失、未知字段或非法值
    均抛 INVALID_HANDOFF_QUERY。
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


def _find_handoff_pairs(transfers, window_seconds):
    """枚举全部跨链交接的有序笔对，返回 (first, second) 列表。

    first 为时间上的首笔（S）、second 为次笔（D）：from(second)==to(first)，
    共同地址即 intermediary；资产相同、链不同，时间差落在闭窗口。同一无序
    笔对检查两个时间方向（两序）；时间相同且两个方向都成立时各成一个事件。
    """
    pairs = []
    for index, first in enumerate(transfers):
        for second in transfers[index + 1:]:
            # 自转账不参与交接；id 互异由 i<j 枚举保证。
            if first["from_address"] == first["to_address"]:
                continue
            if second["from_address"] == second["to_address"]:
                continue
            if first["asset"] != second["asset"]:
                continue
            if first["chain"] == second["chain"]:
                continue

            # 两个时间方向各检查一次（两序）：时间不同则只有较早→较晚方向
            # 可能满足 Δt>=0；时间相同则两个相接方向都可能各成事件。
            for earlier, later in ((first, second), (second, first)):
                if later["from_address"] != earlier["to_address"]:
                    continue
                delta = (later["timestamp"] - earlier["timestamp"]).total_seconds()
                if 0 <= delta <= window_seconds:
                    pairs.append((earlier, later))
    return pairs


def _handoffs(transfers, threshold, config, window_seconds, min_usd_value):
    """枚举跨链交接对并附加 analyze 逐段分值、原因与排序。"""
    scores_by_id = {
        item["id"]: item for item in _score_transfers(transfers, threshold, config)
    }
    raw_pairs = _find_handoff_pairs(transfers, window_seconds)

    handoffs = []
    for first, second in raw_pairs:
        # 美元门槛加在次笔（destination 端），等值保留。
        if float(second["usd_value"]) < min_usd_value:
            continue

        first_score = scores_by_id[first["id"]]
        second_score = scores_by_id[second["id"]]

        dropped = float(second["usd_value"]) < float(first["usd_value"])
        total = (
            float(first_score["score"])
            + float(second_score["score"])
            + ROUND_TRIP_BASE_POINTS
            + (VALUE_DROP_POINTS if dropped else 0.0)
        )
        score = max(0.0, min(100.0, total))

        merged = set(first_score["reason"]) | set(second_score["reason"])
        reason = [HANDOFF_REASON]
        reason.extend(name for name in REASON_BASE_ORDER if name in merged)
        if dropped:
            reason.append(DROP_REASON)

        # transfer_ids/segments 按 (timestamp, id) 排序，event_id=id1>id2。
        ordered = sorted((first, second), key=lambda item: (item["timestamp"], item["id"]))
        transfer_ids = [item["id"] for item in ordered]
        segment_scores = [scores_by_id[item["id"]] for item in ordered]

        handoffs.append(
            {
                "source_address": first["from_address"],
                "source_chain": first["chain"],
                "source_amount": _round10(first["amount"]),
                "source_usd_value": _round10(first["usd_value"]),
                "destination_address": second["to_address"],
                "destination_chain": second["chain"],
                "destination_amount": _round10(second["amount"]),
                "destination_usd_value": _round10(second["usd_value"]),
                "intermediary": first["to_address"],
                "asset": first["asset"],
                "transfer_ids": transfer_ids,
                "event_id": "%s>%s" % (transfer_ids[0], transfer_ids[1]),
                "amount_delta": _round10(
                    float(second["amount"]) - float(first["amount"])
                ),
                "usd_delta": _round10(
                    float(second["usd_value"]) - float(first["usd_value"])
                ),
                "usd_value": _round10(
                    max(float(first["usd_value"]), float(second["usd_value"]))
                ),
                "score": _round10(score),
                "reason": reason,
                "segments": _segments(segment_scores),
            }
        )

    handoffs.sort(key=lambda item: (-item["score"], item["event_id"]))
    return handoffs


def _handoff_alerts(handoffs, routes):
    """每条 route 与每个 event 至多一项告警，按 route_id、event_id 排序。

    chains 需同时命中 source_chain 与 destination_chain 两端；assets 命中
    交接资产；星号规则与其他命令一致。
    """
    alerts = []
    for route in routes:
        # 每个 route/event 至多一项：两个方向等时成单时会共享 event_id，
        # 按 handoffs 的稳定排序取首个命中项。
        seen_events = set()
        for handoff in handoffs:
            if handoff["event_id"] in seen_events:
                continue
            if handoff["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], handoff["source_chain"]):
                continue
            if not _match(route["chains"], handoff["destination_chain"]):
                continue
            if not _match(route["assets"], handoff["asset"]):
                continue
            # handoffs 按 score 降序，故共享 event_id 时取分值最高的命中方向。
            seen_events.add(handoff["event_id"])
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

    handoff_list = _handoffs(
        transfers, threshold, config, window_seconds, min_usd_value
    )
    alerts = _handoff_alerts(handoff_list, routes)
    return {"handoffs": handoff_list, "alerts": alerts}
