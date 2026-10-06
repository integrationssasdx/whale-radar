"""先归集多方资金再分发的分层（layering）事件：``whale-radar layering``。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 layering_query 查询对象），输出 data 仅含
layering、alerts 两个数组；无事件时两者均为空数组。

layering_query 必须恰好含 window_seconds（1..10000 整数）、min_sources
（2..10000 整数）、min_recipients（2..10000 整数）、min_usd_value
（非负有限数）；缺失、含未知字段、布尔或非法值均报
INVALID_LAYERING_QUERY。

对每个 (chain, asset, hub)，取该 hub 作为 to_address 的转入与作为
from_address 的转出（自转账始终忽略，不计来源、接收方或事件），按
(timestamp, id) 排序。以每笔转入的 timestamp 为起点，把 timestamp 落在
[起点, 起点+window_seconds] 闭区间内的全部转入与转出纳入窗口（纯时间戳
过滤，与 id 大小无关）；窗口内不同来源数>=min_sources、不同接收方数
>=min_recipients、窗口内全部转账 usd_value 合计>=min_usd_value 时
生成事件。

事件 transfer_ids 为窗口内全部转账按 (timestamp, id) 排序；同一
(chain, asset, hub) 下 transfer_ids 相同（即起点时间戳相同）的多个起点
只保留起点 id 最小者，该 id 即为 event_id。金额取 10 位小数；score 为
analyze 同一 scoring 配置下窗口内逐笔分值求和后截到 0..100，reason 按
VALUE、BURST、FAN_OUT、ROUND_TRIP 去重；segments 与 transfer_ids 同序，
每项恰含 transfer_id、score、reason，口径与 trace-risk、watch 一致。
layering 按 score 降序、chain、asset、address、event_id 升序排序。

alerts 沿用 cycles 的字段与星号规则（cycle_id 改为 event_id，无 from/to），
每个 route/event 至多一项，按 route_id、event_id 排序。

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

LAYERING_FIELDS = (
    "window_seconds",
    "min_sources",
    "min_recipients",
    "min_usd_value",
)

WINDOW_SECONDS_MIN, WINDOW_SECONDS_MAX = 1, 10000
COUNT_MIN, COUNT_MAX = 2, 10000

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _validate_layering_query(payload):
    """layering_query 校验，返回 (window_seconds, min_sources,
    min_recipients, min_usd_value)。

    必须恰好含四个已知字段：window_seconds 为 1..10000 整数、min_sources 与
    min_recipients 为 2..10000 整数（均不接受 bool）、min_usd_value 为非负
    有限数（不接受 bool）。任何缺失、未知字段或非法值均抛
    INVALID_LAYERING_QUERY。
    """
    if "layering_query" not in payload:
        raise AnalyzeError("INVALID_LAYERING_QUERY")
    raw = payload["layering_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_LAYERING_QUERY")
    if set(raw) != set(LAYERING_FIELDS):
        raise AnalyzeError("INVALID_LAYERING_QUERY")

    window_seconds = raw["window_seconds"]
    min_sources = raw["min_sources"]
    min_recipients = raw["min_recipients"]
    min_usd_value = raw["min_usd_value"]

    if (
        not isinstance(window_seconds, int)
        or isinstance(window_seconds, bool)
        or not (WINDOW_SECONDS_MIN <= window_seconds <= WINDOW_SECONDS_MAX)
    ):
        raise AnalyzeError("INVALID_LAYERING_QUERY")
    for value in (min_sources, min_recipients):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not (COUNT_MIN <= value <= COUNT_MAX)
        ):
            raise AnalyzeError("INVALID_LAYERING_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_LAYERING_QUERY")

    return window_seconds, min_sources, min_recipients, float(min_usd_value)


def _build_events(transfers, threshold, config, window_seconds, min_sources,
                  min_recipients, min_usd_value):
    """按 (chain, asset, hub) 枚举归集再分发事件，附分值、原因、segments。"""
    groups = {}
    for transfer in transfers:
        if transfer["from_address"] == transfer["to_address"]:
            continue
        groups.setdefault(
            (transfer["chain"], transfer["asset"], transfer["to_address"]),
            [],
        ).append(("in", transfer))
        groups.setdefault(
            (transfer["chain"], transfer["asset"], transfer["from_address"]),
            [],
        ).append(("out", transfer))

    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }

    events = []
    for chain, asset, hub in sorted(groups):
        members = sorted(
            groups[(chain, asset, hub)],
            key=lambda member: (member[1]["timestamp"], member[1]["id"]),
        )
        # 同一有序 transfer_ids 仅保留最小起点 id：同 timestamp 的多个转入
        # 起点给出完全相同的窗口，映射到同一键。
        windows = {}
        for _, anchor in members:
            # members 同时含转入与转出；只有转入可作为起点。anchor 来自
            # members 的当前项，需确认它确实是转入 hub 的方向。
            if anchor["to_address"] != hub:
                continue
            window_end = anchor["timestamp"] + timedelta(seconds=window_seconds)
            window = [
                transfer
                for _, transfer in members
                if anchor["timestamp"] <= transfer["timestamp"] <= window_end
            ]
            incoming = [t for t in window if t["to_address"] == hub]
            outgoing = [t for t in window if t["from_address"] == hub]
            sources = {t["from_address"] for t in incoming}
            recipients = {t["to_address"] for t in outgoing}
            usd_total = sum(float(t["usd_value"]) for t in window)
            if (
                len(sources) < min_sources
                or len(recipients) < min_recipients
                or usd_total < min_usd_value
            ):
                continue

            ids = tuple(t["id"] for t in window)
            previous = windows.get(ids)
            if previous is None or anchor["id"] < previous[0]:
                windows[ids] = (anchor["id"], window)

        for ids, (event_id, window) in windows.items():
            incoming = [t for t in window if t["to_address"] == hub]
            outgoing = [t for t in window if t["from_address"] == hub]
            segment_scores = [scores_by_id[tid] for tid in ids]
            total = sum(item["score"] for item in segment_scores)
            merged = set()
            for item in segment_scores:
                merged.update(item["reason"])
            reasons = [reason for reason in REASON_ORDER if reason in merged]

            events.append(
                {
                    "event_id": event_id,
                    "chain": chain,
                    "asset": asset,
                    "address": hub,
                    "transfer_ids": list(ids),
                    "source_count": len({t["from_address"] for t in incoming}),
                    "recipient_count": len(
                        {t["to_address"] for t in outgoing}
                    ),
                    "amount": _round10(
                        sum(float(t["amount"]) for t in window)
                    ),
                    "usd_value": _round10(
                        sum(float(t["usd_value"]) for t in window)
                    ),
                    "score": _round10(min(100.0, total)),
                    "reason": reasons,
                    "segments": _segments(segment_scores),
                }
            )

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
    """对已解析的输入 JSON 执行归集再分发分层分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    (window_seconds, min_sources, min_recipients,
     min_usd_value) = _validate_layering_query(payload)
    config = _validate_scoring(payload)

    events = _build_events(
        transfers,
        threshold,
        config,
        window_seconds,
        min_sources,
        min_recipients,
        min_usd_value,
    )
    alerts = _layering_alerts(events, routes)
    return {"layering": events, "alerts": alerts}
