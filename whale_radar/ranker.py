"""巨鲸地址画像与聚合告警：``whale-radar rank`` 的核心逻辑。

输入为已解析的 JSON 对象（与 analyze 同形：transfers、whale_threshold_usd、
routes），输出 data 仅含 profiles、alerts 两个数组。

校验完全复用 analyzer，错误码与优先级与其一致：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

from datetime import timedelta

from .analyzer import (
    SCORING_DEFAULTS,
    _match,
    _resolve_scoring,
    _round10,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_threshold,
    _validate_values,
)

REASON_WHALE = "WHALE_EXPOSURE"
REASON_COUNTERPARTY = "COUNTERPARTY_DISTRIBUTION"
REASON_ROUND_TRIP = "ROUND_TRIP_ACTIVITY"
REASON_BURST = "BURST_ACTIVITY"


def _build_profiles(transfers, threshold, scoring=None):
    """按 from_address/to_address 去重聚合地址画像并打分。"""
    if scoring is None:
        scoring = SCORING_DEFAULTS
    stats = {}

    def node(address):
        return stats.setdefault(
            address,
            {
                "sent_usd": 0.0,
                "received_usd": 0.0,
                "whale_transfers": 0,
                "counterparties": set(),
                "sent_at": [],
            },
        )

    # (from, to, asset, amount) -> id 集合，双向转账判定口径同 analyze。
    direction_index = {}
    for transfer in transfers:
        key = (
            transfer["from_address"],
            transfer["to_address"],
            transfer["asset"],
            transfer["amount"],
        )
        direction_index.setdefault(key, set()).add(transfer["id"])

    for transfer in transfers:
        frm = transfer["from_address"]
        to = transfer["to_address"]
        usd = float(transfer["usd_value"])
        sender = node(frm)
        receiver = node(to)
        sender["sent_usd"] += usd
        receiver["received_usd"] += usd
        sender["sent_at"].append(transfer["timestamp"])
        is_whale = usd >= threshold
        if is_whale:
            # 巨鲸转账触及双方；自转账只触及自身一次。
            sender["whale_transfers"] += 1
            if to != frm:
                receiver["whale_transfers"] += 1
        # 相反地址去重；自转账的相反地址为自身。
        sender["counterparties"].add(to)
        if to != frm:
            receiver["counterparties"].add(frm)

    profiles = []
    for address in sorted(stats):
        item = stats[address]

        reasons = []
        score = 0
        if item["whale_transfers"] > 0:
            score += scoring["whale_points"]
            reasons.append(REASON_WHALE)
        if len(item["counterparties"]) >= scoring["counterparty_count"]:
            score += scoring["counterparty_points"]
            reasons.append(REASON_COUNTERPARTY)

        has_round_trip = any(
            frm == address
            and to != address
            and (to, address, asset, amount) in direction_index
            for frm, to, asset, amount in direction_index
        )
        if has_round_trip:
            score += scoring["address_round_trip_points"]
            reasons.append(REASON_ROUND_TRIP)

        sent_at = sorted(item["sent_at"])
        window = timedelta(seconds=scoring["window_seconds"])
        has_burst = any(
            sum(
                1
                for other in sent_at
                if start <= other <= start + window
            )
            >= scoring["burst_count"]
            for start in sent_at
        )
        if has_burst:
            score += scoring["address_burst_points"]
            reasons.append(REASON_BURST)

        score = max(0, min(100, score))
        profiles.append(
            {
                "address": address,
                "sent_usd": _round10(item["sent_usd"]),
                "received_usd": _round10(item["received_usd"]),
                "net_usd": _round10(item["sent_usd"] - item["received_usd"]),
                "whale_transfers": item["whale_transfers"],
                "counterparties": len(item["counterparties"]),
                "risk_score": score,
                "reasons": reasons,
            }
        )

    profiles.sort(key=lambda profile: (-profile["risk_score"], profile["address"]))
    return profiles


def _rank_alerts(transfers, routes, profiles):
    """每条 route 与每个地址至多一项告警。"""
    touching = {}
    for transfer in transfers:
        pair = (transfer["chain"], transfer["asset"])
        touching.setdefault(transfer["from_address"], []).append(pair)
        if transfer["to_address"] != transfer["from_address"]:
            touching.setdefault(transfer["to_address"], []).append(pair)

    alerts = []
    for route in routes:
        for profile in profiles:
            address = profile["address"]
            if profile["risk_score"] < route["min_score"]:
                continue
            # 存在一笔触及转账同时命中 chains 与 assets，星号匹配任意。
            matched = any(
                _match(route["chains"], chain)
                and _match(route["assets"], asset)
                for chain, asset in touching.get(address, ())
            )
            if not matched:
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
    """对已解析的输入 JSON 执行巨鲸画像与聚合告警，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    scoring = _resolve_scoring(payload)

    profiles = _build_profiles(transfers, threshold, scoring)
    alerts = _rank_alerts(transfers, routes, profiles)
    return {"profiles": profiles, "alerts": alerts}
