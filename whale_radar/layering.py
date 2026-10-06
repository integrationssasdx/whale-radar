"""先归集后分发的分层（layering）事件：``whale-radar layering`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 layering_query 查询对象），输出 data 仅含
layering、alerts 两个数组；无事件时两者均为空数组。

layering_query 必须恰好含 window_seconds（1..10000 整数）、min_sources
（2..10000 整数）、min_recipients（2..10000 整数）、min_usd_value（非负
有限数）；缺失、含未知字段、布尔或非法值均报 INVALID_LAYERING_QUERY。

转账按 (chain, asset) 分组；组内以每笔转入某中心地址的非自转账的
timestamp 为起点，纳入 [起点, 起点+window_seconds] 闭区间内该地址的转入
与转出构成窗口。窗口内不同来源数>=min_sources、不同接收方数>=min_recipients
且 usd_value 合计>=min_usd_value 时输出事件。自转账不计来源、接收方或
事件（不能作为事件起点，其地址不进入来源/接收方计数）。同一地址产生相同
transfer_ids 的多个起点仅保留最小起点 id 作为 event_id。每项含 event_id、
chain、asset、address、transfer_ids（按 timestamp、id 排序）、
source_count、recipient_count、amount、usd_value、score、reason、
segments；金额与 score 保留 10 位小数。score 为 analyze 逐段分值求和后
截到 0..100，reason 按 VALUE、BURST、FAN_OUT、ROUND_TRIP 去重；segments
与 transfer_ids 同序对应，每项恰含 transfer_id、score、reason。layering
按 score 降序，再按 chain、asset、address、event_id 升序排序。alerts
每个 route/event 至多一项，score>=route.min_score 且 chains、assets 命中
星号规则时生成，字段沿用 cycles 告警（cycle_id 改为 event_id），按
route_id、event_id 排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_LAYERING_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math
from datetime import timedelta

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

LAYERING_QUERY_FIELDS = (
    "window_seconds",
    "min_sources",
    "min_recipients",
    "min_usd_value",
)

WINDOW_SECONDS_MIN, WINDOW_SECONDS_MAX = 1, 10000
MIN_SOURCES_MIN, MIN_SOURCES_MAX = 2, 10000
MIN_RECIPIENTS_MIN, MIN_RECIPIENTS_MAX = 2, 10000

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _validate_layering_query(payload):
    """layering_query 校验，返回四元组查询参数。

    必须恰好含四个已知字段：window_seconds 为 1..10000 整数、min_sources
    与 min_recipients 为 2..10000 整数（均不接受 bool）、min_usd_value 为
    非负有限数（不接受 bool）。任何缺失、未知字段或非法值均抛
    INVALID_LAYERING_QUERY。
    """
    if "layering_query" not in payload:
        raise AnalyzeError("INVALID_LAYERING_QUERY")
    raw = payload["layering_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_LAYERING_QUERY")
    if set(raw) != set(LAYERING_QUERY_FIELDS):
        raise AnalyzeError("INVALID_LAYERING_QUERY")

    window_seconds = raw["window_seconds"]
    min_sources = raw["min_sources"]
    min_recipients = raw["min_recipients"]
    min_usd_value = raw["min_usd_value"]

    for value, low, high in (
        (window_seconds, WINDOW_SECONDS_MIN, WINDOW_SECONDS_MAX),
        (min_sources, MIN_SOURCES_MIN, MIN_SOURCES_MAX),
        (min_recipients, MIN_RECIPIENTS_MIN, MIN_RECIPIENTS_MAX),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not (low <= value <= high)
        ):
            raise AnalyzeError("INVALID_LAYERING_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_LAYERING_QUERY")

    return window_seconds, min_sources, min_recipients, float(min_usd_value)


def _layering_events(transfers, threshold, config, window_seconds,
                     min_sources, min_recipients, min_usd_value):
    """按 (chain, asset) 分组枚举先归集后分发事件并附加分值与原因。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    groups = {}
    for transfer in transfers:
        groups.setdefault((transfer["chain"], transfer["asset"]), []).append(
            transfer
        )

    events = []
    for chain, asset in sorted(groups):
        edges = groups[(chain, asset)]
        addresses = set()
        for transfer in edges:
            addresses.add(transfer["from_address"])
            addresses.add(transfer["to_address"])

        for address in sorted(addresses):
            touching = [
                transfer
                for transfer in edges
                if address in (transfer["from_address"], transfer["to_address"])
            ]
            # 自转账不能作为事件起点。
            anchors = [
                transfer
                for transfer in touching
                if transfer["to_address"] == address
                and transfer["from_address"] != address
            ]
            best = {}
            for anchor in anchors:
                window_end = anchor["timestamp"] + timedelta(
                    seconds=window_seconds
                )
                window = [
                    transfer
                    for transfer in touching
                    if anchor["timestamp"] <= transfer["timestamp"] <= window_end
                ]
                window.sort(
                    key=lambda transfer: (transfer["timestamp"], transfer["id"])
                )
                sources = {
                    transfer["from_address"]
                    for transfer in window
                    if transfer["to_address"] == address
                    and transfer["from_address"] != address
                }
                recipients = {
                    transfer["to_address"]
                    for transfer in window
                    if transfer["from_address"] == address
                    and transfer["to_address"] != address
                }
                usd_value = sum(
                    float(transfer["usd_value"]) for transfer in window
                )
                if (
                    len(sources) < min_sources
                    or len(recipients) < min_recipients
                    or usd_value < min_usd_value
                ):
                    continue

                transfer_ids = [transfer["id"] for transfer in window]
                event_id = anchor["id"]
                key = tuple(transfer_ids)
                # 同址同 transfer_ids 仅留最小起点 id 为 event_id。
                if key in best and best[key]["event_id"] <= event_id:
                    continue

                amount = sum(float(transfer["amount"]) for transfer in window)
                segment_scores = [scores_by_id[tid] for tid in transfer_ids]
                total = sum(item["score"] for item in segment_scores)
                merged = set()
                for item in segment_scores:
                    merged.update(item["reason"])
                reasons = [
                    reason for reason in REASON_ORDER if reason in merged
                ]
                best[key] = {
                    "event_id": event_id,
                    "chain": chain,
                    "asset": asset,
                    "address": address,
                    "transfer_ids": transfer_ids,
                    "source_count": len(sources),
                    "recipient_count": len(recipients),
                    "amount": _round10(amount),
                    "usd_value": _round10(usd_value),
                    "score": _round10(min(100.0, total)),
                    "reason": reasons,
                    "segments": _segments(segment_scores),
                }
            events.extend(best.values())

    events.sort(
        key=lambda event: (
            -event["score"],
            event["chain"],
            event["asset"],
            event["address"],
            event["event_id"],
        )
    )
    return events


def _layering_alerts(events, routes):
    """每条 route 与每个 event 至多一项告警，按 route_id、event_id 排序。"""
    alerts = []
    for route in routes:
        for event in events:
            if event["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], event["chain"]):
                continue
            if not _match(route["assets"], event["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "event_id": event["event_id"],
                    "chain": event["chain"],
                    "asset": event["asset"],
                    "severity": route["severity"],
                    "score": event["score"],
                    "reason": list(event["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["event_id"]))
    return alerts


def layering(payload):
    """对已解析的输入 JSON 执行先归集后分发事件分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    query = _validate_layering_query(payload)
    config = _validate_scoring(payload)

    events = _layering_events(transfers, threshold, config, *query)
    alerts = _layering_alerts(events, routes)
    return {"layering": events, "alerts": alerts}
