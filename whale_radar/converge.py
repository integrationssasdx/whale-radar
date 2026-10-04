"""多来源短时汇入归集事件：``whale-radar converge`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 convergence 查询对象），输出 data 仅含
events、alerts 两个数组；无事件时两者均为空数组。

convergence 必须恰好含 window_seconds（1..10000 整数）、min_sources
（2..10000 整数）、min_usd_value（非负有限数）；缺失、含未知字段、
布尔或非法值均报 INVALID_CONVERGENCE_QUERY。

转账按 (chain, asset, to_address) 分组并去除自转账，组内按 timestamp、id
升序；以每笔到账为起点，纳入 timestamp<=起点+window_seconds 的到账构成
窗口，窗口内不同来源数>=min_sources 且 usd_value 合计>=min_usd_value 时
输出归集事件。事件含 event_id、recipient、chain、asset、source_count、
transfer_ids、amount、usd_value、score、reason；transfer_ids 按组内顺序
排列并以 ``>`` 连接为 event_id，金额与 score 保留 10 位小数。
score=min(100, 20*(source_count-min_sources+1)+50*usd_value/whale_threshold_usd)；
reason 以 FAN_IN 起，usd_value>=whale_threshold_usd 时追加 VALUE。
events 按 score 降序、event_id 升序排序。alerts 每个 route/event 至多
一项，score>=route.min_score 且 chains、assets 命中星号规则时生成，字段
沿用 watch 告警（path_id、from_address 改为 event_id、recipient），按
route_id、event_id 排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_CONVERGENCE_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math
from datetime import timedelta

from .analyzer import (
    AnalyzeError,
    _is_number,
    _match,
    _round10,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)

CONVERGENCE_FIELDS = ("window_seconds", "min_sources", "min_usd_value")

WINDOW_SECONDS_MIN, WINDOW_SECONDS_MAX = 1, 10000
MIN_SOURCES_MIN, MIN_SOURCES_MAX = 2, 10000


def _validate_convergence(payload):
    """convergence 查询校验，返回 (window_seconds, min_sources, min_usd_value)。

    必须恰好含三个已知字段：window_seconds 为 1..10000 整数、min_sources
    为 2..10000 整数（均不接受 bool）、min_usd_value 为非负有限数（不接受
    bool）。任何缺失、未知字段或非法值均抛 INVALID_CONVERGENCE_QUERY。
    """
    if "convergence" not in payload:
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")
    raw = payload["convergence"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")
    if set(raw) != set(CONVERGENCE_FIELDS):
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")

    window_seconds = raw["window_seconds"]
    min_sources = raw["min_sources"]
    min_usd_value = raw["min_usd_value"]

    if (
        not isinstance(window_seconds, int)
        or isinstance(window_seconds, bool)
        or not (WINDOW_SECONDS_MIN <= window_seconds <= WINDOW_SECONDS_MAX)
    ):
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")
    if (
        not isinstance(min_sources, int)
        or isinstance(min_sources, bool)
        or not (MIN_SOURCES_MIN <= min_sources <= MIN_SOURCES_MAX)
    ):
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_CONVERGENCE_QUERY")

    return window_seconds, min_sources, float(min_usd_value)


def _converge_events(transfers, threshold, window_seconds, min_sources,
                     min_usd_value):
    """按 (chain, asset, to_address) 分组枚举归集事件并附加分值与原因。"""
    groups = {}
    for transfer in transfers:
        if transfer["from_address"] == transfer["to_address"]:
            continue
        key = (transfer["chain"], transfer["asset"], transfer["to_address"])
        groups.setdefault(key, []).append(transfer)

    events = []
    for chain, asset, recipient in sorted(groups):
        incoming = sorted(
            groups[(chain, asset, recipient)],
            key=lambda transfer: (transfer["timestamp"], transfer["id"]),
        )
        for index, start in enumerate(incoming):
            window_end = start["timestamp"] + timedelta(seconds=window_seconds)
            window = [
                transfer
                for transfer in incoming[index:]
                if transfer["timestamp"] <= window_end
            ]
            sources = {transfer["from_address"] for transfer in window}
            usd_value = sum(float(transfer["usd_value"]) for transfer in window)
            if len(sources) < min_sources or usd_value < min_usd_value:
                continue

            amount = sum(float(transfer["amount"]) for transfer in window)
            transfer_ids = [transfer["id"] for transfer in window]
            score = min(
                100.0,
                20.0 * (len(sources) - min_sources + 1)
                + 50.0 * usd_value / threshold,
            )
            reason = ["FAN_IN"]
            if usd_value >= threshold:
                reason.append("VALUE")
            events.append(
                {
                    "event_id": ">".join(transfer_ids),
                    "recipient": recipient,
                    "chain": chain,
                    "asset": asset,
                    "source_count": len(sources),
                    "transfer_ids": transfer_ids,
                    "amount": _round10(amount),
                    "usd_value": _round10(usd_value),
                    "score": _round10(score),
                    "reason": reason,
                }
            )

    events.sort(key=lambda event: (-event["score"], event["event_id"]))
    return events


def _converge_alerts(events, routes):
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
                    "recipient": event["recipient"],
                    "to_address": event["recipient"],
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


def converge(payload):
    """对已解析的输入 JSON 执行多来源归集事件分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    window_seconds, min_sources, min_usd_value = _validate_convergence(payload)
    _validate_scoring(payload)

    events = _converge_events(
        transfers, threshold, window_seconds, min_sources, min_usd_value
    )
    alerts = _converge_alerts(events, routes)
    return {"events": events, "alerts": alerts}
