"""显式地址实体画像与聚合告警：``whale-radar entity`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring），外加必填的 ``entities`` 列表，输出 data 仅含
profiles、alerts 两个数组。

entities 每项只含 id（非空字符串、全局唯一）与 addresses（非空字符串的
非空列表，项内互异、跨实体不重复，且覆盖全部转账地址）；缺项、含未知
字段或关系非法均抛 INVALID_ENTITY_QUERY。

每笔转账按两端地址映射实体：实体内部转账双边累计收发金额、巨鲸次数与
发送时间，但不产生对手；跨实体转账双方分别累计发送或接收，巨鲸转账双方
各计一次。画像沿用 rank 的字段与原因（WHALE_EXPOSURE、
COUNTERPARTY_DISTRIBUTION、ROUND_TRIP_ACTIVITY、BURST_ACTIVITY），
address 改为 entity_id 并增列 addresses（升序），counterparties 为互异
实体 id 数组；往返限不同实体同资产同 amount 正反向，突发按发送时间闭
窗口与 burst_count；risk_score 限制 0..100，profiles 按分值降序、
entity_id 升序。alerts 沿用 rank 字段，address 改为 entity_id 并增列
score，每个 route 与 entity 至多一项，按 route_id、entity_id 升序。

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

REASON_WHALE = "WHALE_EXPOSURE"
REASON_COUNTERPARTY = "COUNTERPARTY_DISTRIBUTION"
REASON_ROUND_TRIP = "ROUND_TRIP_ACTIVITY"
REASON_BURST = "BURST_ACTIVITY"

ENTITY_FIELDS = ("id", "addresses")


def _validate_entities(payload, transfers):
    """entities 校验，返回 (实体定义, 地址 -> entity_id 映射)。

    entities 为必填列表；每项恰含 id、addresses：id 为非空字符串且全局
    唯一，addresses 为非空字符串的非空列表，项内互异、不跨实体重复，且
    覆盖全部转账的两端地址。缺项、未知字段或关系非法均抛
    INVALID_ENTITY_QUERY。
    """
    if "entities" not in payload:
        raise AnalyzeError("INVALID_ENTITY_QUERY")
    raw_entities = payload["entities"]
    if not isinstance(raw_entities, list) or not raw_entities:
        raise AnalyzeError("INVALID_ENTITY_QUERY")

    entities = []
    seen_ids = set()
    address_owner = {}
    for item in raw_entities:
        if not isinstance(item, dict):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if set(item) != set(ENTITY_FIELDS):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        entity_id = item["id"]
        addresses = item["addresses"]
        if not isinstance(entity_id, str) or not entity_id:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if entity_id in seen_ids:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if not isinstance(addresses, list) or not addresses:
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if not all(isinstance(address, str) and address for address in addresses):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        if len(set(addresses)) != len(addresses):
            raise AnalyzeError("INVALID_ENTITY_QUERY")
        for address in addresses:
            if address in address_owner:
                raise AnalyzeError("INVALID_ENTITY_QUERY")
            address_owner[address] = entity_id
        seen_ids.add(entity_id)
        entities.append({"id": entity_id, "addresses": list(addresses)})

    transfer_addresses = set()
    for transfer in transfers:
        transfer_addresses.add(transfer["from_address"])
        transfer_addresses.add(transfer["to_address"])
    if not transfer_addresses.issubset(address_owner):
        raise AnalyzeError("INVALID_ENTITY_QUERY")

    return entities, address_owner


def _build_profiles(transfers, entities, address_owner, threshold, config):
    """按实体聚合地址画像并打分，口径与 rank 地址画像一致。"""
    stats = {
        entity["id"]: {
            "addresses": entity["addresses"],
            "sent_usd": 0.0,
            "received_usd": 0.0,
            "whale_transfers": 0,
            "counterparties": set(),
            "sent_at": [],
        }
        for entity in entities
    }

    # (from_entity, to_entity, asset, amount) -> id 集合，往返判定口径同
    # rank，仅两端映射到不同实体时成立。
    direction_index = {}
    for transfer in transfers:
        key = (
            address_owner[transfer["from_address"]],
            address_owner[transfer["to_address"]],
            transfer["asset"],
            transfer["amount"],
        )
        direction_index.setdefault(key, set()).add(transfer["id"])

    for transfer in transfers:
        frm = address_owner[transfer["from_address"]]
        to = address_owner[transfer["to_address"]]
        usd = float(transfer["usd_value"])
        sender = stats[frm]
        receiver = stats[to]
        sender["sent_usd"] += usd
        receiver["received_usd"] += usd
        sender["sent_at"].append(transfer["timestamp"])
        if usd >= threshold:
            # 巨鲸转账触及双方；实体内部转账只触及该实体一次。
            sender["whale_transfers"] += 1
            if to != frm:
                receiver["whale_transfers"] += 1
        # 实体内部转账不计对手；跨实体双方互记对方实体。
        if to != frm:
            sender["counterparties"].add(to)
            receiver["counterparties"].add(frm)

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
                "addresses": sorted(item["addresses"]),
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


def _entity_alerts(transfers, routes, profiles, address_owner):
    """每条 route 与每个实体至多一项告警。"""
    touching = {}
    for transfer in transfers:
        pair = (transfer["chain"], transfer["asset"])
        frm = address_owner[transfer["from_address"]]
        to = address_owner[transfer["to_address"]]
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

    alerts.sort(key=lambda alert: (alert["route_id"], alert["entity_id"]))
    return alerts


def entity(payload):
    """对已解析的输入 JSON 执行实体画像与聚合告警，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    entities, address_owner = _validate_entities(payload, transfers)
    config = _validate_scoring(payload)

    profiles = _build_profiles(
        transfers, entities, address_owner, threshold, config
    )
    alerts = _entity_alerts(transfers, routes, profiles, address_owner)
    return {"profiles": profiles, "alerts": alerts}
