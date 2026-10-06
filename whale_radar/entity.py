"""实体归并画像与聚合告警：``whale-radar entity`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes 与可选 scoring），外加必填的 ``entities`` 归并表，输出 data 仅含
profiles、alerts 两个数组；无告警时 alerts 为空数组。

entities 为非空列表，每项恰含 id（非空字符串、全局唯一）与 addresses
（非空列表，元素为互异非空字符串）；所有地址不跨实体重复，且必须恰好
覆盖全部转账的 from/to 地址。缺项、未知字段或非法关系均报
INVALID_ENTITY_QUERY。

每笔转账按两端地址映射到实体：同实体转账双边各累计一次收发金额、巨鲸
次数与发送时间，但不产生 counterparty；跨实体转账双方分别累计发送或
接收，巨鲸转账双方各计一次巨鲸，counterparties 记录互异的对端实体 id。
画像字段沿用 rank：address 改为 entity_id 并增列 addresses，
counterparties 为互异实体 id 数组，地址升序排列，金额与分值保留 10 位
小数；原因沿用 rank 的实体巨鲸、对手分布、往返与突发四项，往返限不同
实体间同资产同 amount 的正反向转账，突发按发送时间闭窗口与 burst_count
判定，risk_score 限制在 0..100。profiles 按 risk_score 降序、entity_id
升序排序。alerts 每个 route 与 entity 至多一项，实体涉及转账的 chain、
asset 命中 route 星号规则且 risk_score 不低于 min_score 时生成，reason
取实体 reasons，按 route_id、entity_id 升序排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_ENTITY_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

from datetime import timedelta

from .analyzer import (
    AnalyzeError,
    _match,
    _round10,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)
from .ranker import (
    REASON_BURST,
    REASON_COUNTERPARTY,
    REASON_ROUND_TRIP,
    REASON_WHALE,
)

ENTITY_FIELDS = ("id", "addresses")


def _validate_entities(payload, transfers):
    """entities 归并表校验，返回 entity_id -> 排序地址列表的映射。

    每项恰含非空唯一 id 与非空互异非空字符串地址列表；地址不跨实体重复，
    且恰好覆盖全部转账两端地址。任何缺失、未知字段或非法关系均抛
    INVALID_ENTITY_QUERY。
    """
    if "entities" not in payload:
        raise AnalyzeError("INVALID_ENTITY_QUERY")
    raw = payload["entities"]
    if not isinstance(raw, list) or not raw:
        raise AnalyzeError("INVALID_ENTITY_QUERY")

    entities = {}
    assigned = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != set(ENTITY_FIELDS):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        entity_id = item["id"]
        addresses = item["addresses"]
        if not isinstance(entity_id, str) or not entity_id:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if entity_id in entities:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if not isinstance(addresses, list) or not addresses:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if not all(isinstance(address, str) and address for address in addresses):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if len(set(addresses)) != len(addresses):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        for address in addresses:
            if address in assigned:
                raise AnalyzeError("INVALID_ENTITY_QUERY")
            assigned[address] = entity_id
        entities[entity_id] = sorted(addresses)

    transfer_addresses = set()
    for transfer in transfers:
        transfer_addresses.add(transfer["from_address"])
        transfer_addresses.add(transfer["to_address"])
    if set(assigned) != transfer_addresses:
        raise AnalyzeError("INVALID_ENTITY_QUERY")

    return entities, assigned


def _build_entity_profiles(transfers, threshold, config, entities, assigned):
    """按实体聚合收发金额、巨鲸次数、对手实体、往返与突发并打分。"""
    stats = {}

    def node(entity_id):
        return stats.setdefault(
            entity_id,
            {
                "sent_usd": 0.0,
                "received_usd": 0.0,
                "whale_transfers": 0,
                "counterparties": set(),
                "sent_at": [],
            },
        )

    # (from_entity, to_entity, asset, amount) -> id 集合，跨实体往返判定。
    direction_index = {}
    for transfer in transfers:
        frm = assigned[transfer["from_address"]]
        to = assigned[transfer["to_address"]]
        if frm == to:
            continue
        key = (frm, to, transfer["asset"], transfer["amount"])
        direction_index.setdefault(key, set()).add(transfer["id"])

    for transfer in transfers:
        frm = assigned[transfer["from_address"]]
        to = assigned[transfer["to_address"]]
        usd = float(transfer["usd_value"])
        sender = node(frm)
        sender["sent_usd"] += usd
        sender["sent_at"].append(transfer["timestamp"])
        receiver = node(to)
        receiver["received_usd"] += usd
        is_whale = usd >= threshold
        if frm == to:
            # 同实体双边累计：收发各一次，巨鲸与发送时间不重复计对手端。
            if is_whale:
                sender["whale_transfers"] += 1
        else:
            sender["counterparties"].add(to)
            receiver["counterparties"].add(frm)
            if is_whale:
                # 巨鲸转账触及双方实体，各计一次。
                sender["whale_transfers"] += 1
                receiver["whale_transfers"] += 1

    profiles = []
    for entity_id in sorted(stats):
        item = stats[entity_id]

        reasons = []
        score = 0
        if item["whale_transfers"] > 0:
            score += config["whale_points"]
            reasons.append(REASON_WHALE)
        if len(item["counterparties"]) >= config["counterparty_count"]:
            score += config["counterparty_points"]
            reasons.append(REASON_COUNTERPARTY)

        has_round_trip = any(
            frm == entity_id
            and to != entity_id
            and (to, entity_id, asset, amount) in direction_index
            for frm, to, asset, amount in direction_index
        )
        if has_round_trip:
            score += config["address_round_trip_points"]
            reasons.append(REASON_ROUND_TRIP)

        sent_at = sorted(item["sent_at"])
        has_burst = any(
            sum(
                1
                for other in sent_at
                if start <= other <= start + timedelta(
                    seconds=config["window_seconds"]
                )
            )
            >= config["burst_count"]
            for start in sent_at
        )
        if has_burst:
            score += config["address_burst_points"]
            reasons.append(REASON_BURST)

        score = _round10(max(0, min(100, score)))
        profiles.append(
            {
                "entity_id": entity_id,
                "addresses": entities[entity_id],
                "sent_usd": _round10(item["sent_usd"]),
                "received_usd": _round10(item["received_usd"]),
                "net_usd": _round10(item["sent_usd"] - item["received_usd"]),
                "whale_transfers": item["whale_transfers"],
                "counterparties": sorted(item["counterparties"]),
                "risk_score": score,
                "reasons": reasons,
            }
        )

    profiles.sort(
        key=lambda profile: (-profile["risk_score"], profile["entity_id"])
    )
    return profiles


def _entity_alerts(transfers, routes, profiles, assigned):
    """每条 route 与每个实体至多一项告警，按 route_id、entity_id 排序。"""
    touching = {}
    for transfer in transfers:
        pair = (transfer["chain"], transfer["asset"])
        frm = assigned[transfer["from_address"]]
        to = assigned[transfer["to_address"]]
        touching.setdefault(frm, []).append(pair)
        if to != frm:
            touching.setdefault(to, []).append(pair)

    alerts = []
    for route in routes:
        for profile in profiles:
            entity_id = profile["entity_id"]
            if profile["risk_score"] < route["min_score"]:
                continue
            # 存在一笔触及转账同时命中 chains 与 assets，星号匹配任意。
            matched = any(
                _match(route["chains"], chain)
                and _match(route["assets"], asset)
                for chain, asset in touching.get(entity_id, ())
            )
            if not matched:
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "entity_id": entity_id,
                    "severity": route["severity"],
                    "score": profile["risk_score"],
                    "reason": list(profile["reasons"]),
                    "target": route["target"],
                }
            )

    alerts.sort(
        key=lambda alert: (alert["route_id"], alert["entity_id"])
    )
    return alerts


def entity(payload):
    """对已解析的输入 JSON 执行实体归并画像与告警，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    entities, assigned = _validate_entities(payload, transfers)
    config = _validate_scoring(payload)

    profiles = _build_entity_profiles(
        transfers, threshold, config, entities, assigned
    )
    alerts = _entity_alerts(transfers, routes, profiles, assigned)
    return {"profiles": profiles, "alerts": alerts}
