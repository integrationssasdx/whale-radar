"""巨鲸风险画像与路由告警聚合。

输入为已解析的 JSON 对象，输出为可 JSON 序列化的 dict。
任何输入错误都抛出 ``AnalyzeError(code)``，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
"""

from __future__ import annotations

from datetime import timedelta

from .analyzer import (
    _match,
    _round10,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_threshold,
    _validate_values,
)

WINDOW_SECONDS = 3600

POINTS_WHALE_EXPOSURE = 40.0
POINTS_COUNTERPARTY_DISTRIBUTION = 25.0
POINTS_ROUND_TRIP_ACTIVITY = 20.0
POINTS_BURST_ACTIVITY = 15.0

BURST_COUNT = 5
COUNTERPARTY_MIN = 3

REASON_WHALE = "WHALE_EXPOSURE"
REASON_COUNTERPARTY = "COUNTERPARTY_DISTRIBUTION"
REASON_ROUND_TRIP = "ROUND_TRIP_ACTIVITY"
REASON_BURST = "BURST_ACTIVITY"

REASON_ORDER = (
    REASON_WHALE,
    REASON_COUNTERPARTY,
    REASON_ROUND_TRIP,
    REASON_BURST,
)


def _build_profiles(transfers, threshold):
    """按地址聚合画像：资金量、巨鲸触及、对手方与风险评分。"""
    profiles = {}

    def profile(address):
        return profiles.setdefault(
            address,
            {
                "address": address,
                "sent_usd": 0.0,
                "received_usd": 0.0,
                "whale_transfers": 0,
                "counterparty_set": set(),
                "sent_timestamps": [],
            },
        )

    for transfer in transfers:
        sender = profile(transfer["from_address"])
        receiver = profile(transfer["to_address"])
        usd_value = float(transfer["usd_value"])
        sender["sent_usd"] += usd_value
        receiver["received_usd"] += usd_value
        if usd_value >= threshold:
            # 自转账只触及一个地址，按一笔计。
            sender["whale_transfers"] += 1
            if transfer["to_address"] != transfer["from_address"]:
                receiver["whale_transfers"] += 1
        sender["counterparty_set"].add(transfer["to_address"])
        receiver["counterparty_set"].add(transfer["from_address"])
        sender["sent_timestamps"].append(transfer["timestamp"])

    # 同 asset 等 amount 的反向转账索引：(to, from, asset, amount) -> True
    reverse_index = set()
    for transfer in transfers:
        reverse_index.add(
            (
                transfer["from_address"],
                transfer["to_address"],
                transfer["asset"],
                transfer["amount"],
            )
        )

    result = []
    for address in profiles:
        item = profiles[address]
        sent_usd = item["sent_usd"]
        received_usd = item["received_usd"]
        counterparties = len(item["counterparty_set"])

        reasons = []
        total = 0.0
        if item["whale_transfers"] > 0:
            total += POINTS_WHALE_EXPOSURE
            reasons.append(REASON_WHALE)
        if counterparties >= COUNTERPARTY_MIN:
            total += POINTS_COUNTERPARTY_DISTRIBUTION
            reasons.append(REASON_COUNTERPARTY)
        if any(
            transfer["from_address"] != transfer["to_address"]
            and (
                transfer["to_address"],
                transfer["from_address"],
                transfer["asset"],
                transfer["amount"],
            )
            in reverse_index
            for transfer in transfers
            if address in (transfer["from_address"], transfer["to_address"])
        ):
            total += POINTS_ROUND_TRIP_ACTIVITY
            reasons.append(REASON_ROUND_TRIP)
        if any(
            sum(
                1
                for other in item["sent_timestamps"]
                if start <= other <= start + timedelta(seconds=WINDOW_SECONDS)
            )
            >= BURST_COUNT
            for start in item["sent_timestamps"]
        ):
            total += POINTS_BURST_ACTIVITY
            reasons.append(REASON_BURST)

        result.append(
            {
                "address": address,
                "sent_usd": _round10(sent_usd),
                "received_usd": _round10(received_usd),
                "net_usd": _round10(sent_usd - received_usd),
                "whale_transfers": item["whale_transfers"],
                "counterparties": counterparties,
                "risk_score": _round10(max(0.0, min(100.0, total))),
                "reasons": reasons,
            }
        )

    result.sort(key=lambda item: (-item["risk_score"], item["address"]))
    return result


def _route_alerts(transfers, routes, profiles):
    """每条 route 与命中地址至多一条告警，按 route_id、address 排序。"""
    # 地址涉及的全部 (chain, asset) 组合，用于路由命中判定。
    touched = {}
    for transfer in transfers:
        pair = (transfer["chain"], transfer["asset"])
        touched.setdefault(transfer["from_address"], set()).add(pair)
        touched.setdefault(transfer["to_address"], set()).add(pair)

    alerts = []
    for route in routes:
        for profile in profiles:
            if profile["risk_score"] < route["min_score"]:
                continue
            address = profile["address"]
            if not any(
                _match(route["chains"], chain) and _match(route["assets"], asset)
                for chain, asset in touched.get(address, ())
            ):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "address": address,
                    "severity": route["severity"],
                    "reason": list(profile["reasons"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["address"]))
    return alerts


def rank(payload):
    """对已解析的输入 JSON 执行风险画像与告警聚合，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)

    profiles = _build_profiles(transfers, threshold)
    alerts = _route_alerts(transfers, routes, profiles)
    return {"profiles": profiles, "alerts": alerts}
