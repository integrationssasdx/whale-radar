"""核心分析逻辑：校验、资金流图、巨鲸筛选、异常打分、告警路由。

输入为已解析的 JSON 对象，输出为可 JSON 序列化的 dict。
任何输入错误都抛出 ``AnalyzeError(code)``，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# 可配置异常分值的基线默认值；scoring 中省略的字段沿用这些值。
DEFAULT_SCORING = {
    "window_seconds": 3600,
    "burst_count": 5,
    "fan_out_recipients": 3,
    "value_points_per_ratio": 20.0,
    "value_points_cap": 40.0,
    "transfer_burst_points": 25.0,
    "fan_out_points": 20.0,
    "round_trip_points": 15.0,
    "whale_points": 40,
    "counterparty_count": 3,
    "counterparty_points": 25,
    "address_round_trip_points": 20,
    "address_burst_points": 15,
}

# 窗口与数量阈值：1..10000 的整数（不接受 bool）。
SCORING_INT_FIELDS = ("window_seconds", "burst_count", "fan_out_recipients",
                      "counterparty_count")
# value_points_per_ratio：0..1000 的有限数（不接受 bool）。
SCORING_RATIO_FIELDS = ("value_points_per_ratio",)
# 其余分值与 value_points_cap：0..100 的有限数（不接受 bool）。
SCORING_POINT_FIELDS = (
    "value_points_cap",
    "transfer_burst_points",
    "fan_out_points",
    "round_trip_points",
    "whale_points",
    "counterparty_points",
    "address_round_trip_points",
    "address_burst_points",
)
SCORING_FIELDS = (
    SCORING_INT_FIELDS + SCORING_RATIO_FIELDS + SCORING_POINT_FIELDS
)

REASON_VALUE = "VALUE"
REASON_BURST = "BURST"
REASON_FAN_OUT = "FAN_OUT"
REASON_ROUND_TRIP = "ROUND_TRIP"

SEVERITIES = ("info", "warning", "critical")

TRANSFER_FIELDS = (
    "id",
    "timestamp",
    "chain",
    "asset",
    "from_address",
    "to_address",
    "amount",
    "usd_value",
)
ROUTE_FIELDS = ("id", "min_score", "severity", "chains", "assets", "target")


class AnalyzeError(Exception):
    """携带对外错误码的分析异常。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_timestamp(value):
    """RFC3339（带时区）字符串 -> UTC datetime；失败由调用方归类。"""
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _validate_schema(payload):
    """结构与字段类型校验。返回 (transfers, threshold, routes) 原始值。"""
    if not isinstance(payload, dict):
        raise AnalyzeError("INVALID_INPUT_SCHEMA")

    for key in ("transfers", "whale_threshold_usd", "routes"):
        if key not in payload:
            raise AnalyzeError("INVALID_INPUT_SCHEMA")

    transfers = payload["transfers"]
    threshold = payload["whale_threshold_usd"]
    routes = payload["routes"]

    if not isinstance(routes, list):
        raise AnalyzeError("INVALID_INPUT_SCHEMA")
    # 阈值存在但类型不对属于结构错误；数值范围在后续阶段判定。
    if not _is_number(threshold):
        raise AnalyzeError("INVALID_INPUT_SCHEMA")

    _validate_transfers(transfers)

    for route in routes:
        if not isinstance(route, dict):
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        for field in ROUTE_FIELDS:
            if field not in route:
                raise AnalyzeError("INVALID_INPUT_SCHEMA")
        if not isinstance(route["id"], str) or not isinstance(route["target"], str):
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        if not isinstance(route["severity"], str):
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        if not _is_number(route["min_score"]):
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        for field in ("chains", "assets"):
            if not isinstance(route[field], list):
                raise AnalyzeError("INVALID_INPUT_SCHEMA")

    return transfers, float(threshold), routes


def _validate_transfers(transfers):
    """转账列表结构校验，analyze 与 trace 共用。"""
    if not isinstance(transfers, list):
        raise AnalyzeError("INVALID_INPUT_SCHEMA")
    for transfer in transfers:
        if not isinstance(transfer, dict):
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        for field in TRANSFER_FIELDS:
            if field not in transfer:
                raise AnalyzeError("INVALID_INPUT_SCHEMA")
        for field in ("id", "chain", "asset", "from_address", "to_address"):
            value = transfer[field]
            if not isinstance(value, str) or not value:
                raise AnalyzeError("INVALID_INPUT_SCHEMA")
        if not isinstance(transfer["timestamp"], str) or not transfer["timestamp"]:
            raise AnalyzeError("INVALID_INPUT_SCHEMA")
        for field in ("amount", "usd_value"):
            if not _is_number(transfer[field]):
                raise AnalyzeError("INVALID_INPUT_SCHEMA")


def _validate_duplicates(transfers):
    seen = set()
    for transfer in transfers:
        transfer_id = transfer["id"]
        if transfer_id in seen:
            raise AnalyzeError("DUPLICATE_TRANSFER_ID")
        seen.add(transfer_id)


def _validate_values(transfers):
    """时间与数值范围校验，返回附 UTC datetime 的标准化记录。"""
    normalized = []
    for transfer in transfers:
        ts = _parse_timestamp(transfer["timestamp"])
        if ts is None:
            raise AnalyzeError("INVALID_TRANSFER_VALUE")
        amount = transfer["amount"]
        usd_value = transfer["usd_value"]
        if not math.isfinite(float(amount)) or amount <= 0:
            raise AnalyzeError("INVALID_TRANSFER_VALUE")
        if not math.isfinite(float(usd_value)) or usd_value < 0:
            raise AnalyzeError("INVALID_TRANSFER_VALUE")
        normalized.append(
            {
                "id": transfer["id"],
                "timestamp": ts,
                "chain": transfer["chain"],
                "asset": transfer["asset"],
                "from_address": transfer["from_address"],
                "to_address": transfer["to_address"],
                "amount": amount,
                "usd_value": usd_value,
                "raw": transfer,
            }
        )
    return normalized


def _validate_threshold(threshold):
    if not math.isfinite(threshold) or threshold <= 0:
        raise AnalyzeError("INVALID_THRESHOLD")


def _validate_routes(routes):
    for route in routes:
        if not route["id"]:
            raise AnalyzeError("INVALID_ROUTE")
        min_score = route["min_score"]
        if not math.isfinite(float(min_score)) or not (0 <= min_score <= 100):
            raise AnalyzeError("INVALID_ROUTE")
        if route["severity"] not in SEVERITIES:
            raise AnalyzeError("INVALID_ROUTE")
        for field in ("chains", "assets"):
            values = route[field]
            if not values:
                raise AnalyzeError("INVALID_ROUTE")
            if not all(isinstance(v, str) and v for v in values):
                raise AnalyzeError("INVALID_ROUTE")
        if not route["target"]:
            raise AnalyzeError("INVALID_ROUTE")


def _validate_scoring(payload):
    """scoring 配置校验：必须是对象、字段已知且类型/范围合法。

    可部分提供，缺失字段沿用 DEFAULT_SCORING；返回完整的配置 dict。
    该项在既有校验之后触发，任何不合法都抛 INVALID_SCORING_CONFIG。
    """
    if "scoring" not in payload:
        return dict(DEFAULT_SCORING)
    raw = payload["scoring"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_SCORING_CONFIG")
    unknown = set(raw) - set(SCORING_FIELDS)
    if unknown:
        raise AnalyzeError("INVALID_SCORING_CONFIG")

    config = dict(DEFAULT_SCORING)
    for field in SCORING_INT_FIELDS:
        if field in raw:
            value = raw[field]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not (1 <= value <= 10000)
            ):
                raise AnalyzeError("INVALID_SCORING_CONFIG")
            config[field] = value
    for field in SCORING_RATIO_FIELDS:
        if field in raw:
            value = raw[field]
            if (
                not _is_number(value)
                or not math.isfinite(float(value))
                or not (0 <= float(value) <= 1000)
            ):
                raise AnalyzeError("INVALID_SCORING_CONFIG")
            config[field] = float(value)
    for field in SCORING_POINT_FIELDS:
        if field in raw:
            value = raw[field]
            if (
                not _is_number(value)
                or not math.isfinite(float(value))
                or not (0 <= float(value) <= 100)
            ):
                raise AnalyzeError("INVALID_SCORING_CONFIG")
            config[field] = float(value)
    return config


def _build_graph(transfers):
    nodes = {}
    edges = {}

    def node(address):
        return nodes.setdefault(
            address,
            {
                "address": address,
                "sent_amount": 0.0,
                "received_amount": 0.0,
                "sent_usd": 0.0,
                "received_usd": 0.0,
                "sent_count": 0,
                "received_count": 0,
                "ids": set(),
            },
        )

    for transfer in transfers:
        sender = node(transfer["from_address"])
        receiver = node(transfer["to_address"])
        sender["sent_amount"] += float(transfer["amount"])
        sender["sent_usd"] += float(transfer["usd_value"])
        sender["sent_count"] += 1
        sender["ids"].add(transfer["id"])
        receiver["received_amount"] += float(transfer["amount"])
        receiver["received_usd"] += float(transfer["usd_value"])
        receiver["received_count"] += 1
        receiver["ids"].add(transfer["id"])

        key = (transfer["from_address"], transfer["to_address"])
        edge = edges.setdefault(
            key,
            {
                "from_address": transfer["from_address"],
                "to_address": transfer["to_address"],
                "amount": 0.0,
                "usd_value": 0.0,
                "count": 0,
                "ids": [],
            },
        )
        edge["amount"] += float(transfer["amount"])
        edge["usd_value"] += float(transfer["usd_value"])
        edge["count"] += 1
        edge["ids"].append(transfer["id"])

    node_list = []
    for address in sorted(nodes):
        item = nodes[address]
        item["sent_amount"] = _round10(item["sent_amount"])
        item["received_amount"] = _round10(item["received_amount"])
        item["sent_usd"] = _round10(item["sent_usd"])
        item["received_usd"] = _round10(item["received_usd"])
        item["ids"] = sorted(item["ids"])
        node_list.append(item)

    edge_list = []
    for key in sorted(edges):
        edge = edges[key]
        edge["amount"] = _round10(edge["amount"])
        edge["usd_value"] = _round10(edge["usd_value"])
        edge["ids"] = sorted(edge["ids"])
        edge_list.append(edge)

    return {"nodes": node_list, "edges": edge_list}


def _round10(value):
    """消除浮点累加噪声，保留 10 位小数以内的原值。"""
    rounded = round(float(value), 10)
    return int(rounded) if rounded.is_integer() else rounded


def _score_transfers(transfers, threshold, config):
    # 反向、同资产、等 amount 的转账索引：(to, from, asset, amount) -> id 集合
    reverse_index = {}
    for transfer in transfers:
        key = (
            transfer["from_address"],
            transfer["to_address"],
            transfer["asset"],
            transfer["amount"],
        )
        reverse_index.setdefault(key, set()).add(transfer["id"])

    scores = []
    for transfer in transfers:
        reasons = []
        total = 0.0

        value_points = min(
            float(transfer["usd_value"]) / threshold
            * config["value_points_per_ratio"],
            config["value_points_cap"],
        )
        if value_points > 0:
            total += value_points
            reasons.append(REASON_VALUE)

        window_end = transfer["timestamp"] + timedelta(
            seconds=config["window_seconds"]
        )
        window = [
            other
            for other in transfers
            if other["from_address"] == transfer["from_address"]
            and transfer["timestamp"] <= other["timestamp"] <= window_end
        ]
        if len(window) >= config["burst_count"]:
            total += config["transfer_burst_points"]
            reasons.append(REASON_BURST)
        if len({other["to_address"] for other in window}) >= config[
            "fan_out_recipients"
        ]:
            total += config["fan_out_points"]
            reasons.append(REASON_FAN_OUT)

        reverse_key = (
            transfer["to_address"],
            transfer["from_address"],
            transfer["asset"],
            transfer["amount"],
        )
        is_round_trip = (
            transfer["from_address"] != transfer["to_address"]
            and reverse_key in reverse_index
        )
        if is_round_trip:
            total += config["round_trip_points"]
            reasons.append(REASON_ROUND_TRIP)

        score = max(0.0, min(100.0, total))
        scores.append(
            {
                "id": transfer["id"],
                "score": _round10(score),
                "reason": reasons,
            }
        )

    scores.sort(key=lambda item: (-item["score"], item["id"]))
    return scores


def _match(patterns, raw_value):
    return "*" in patterns or raw_value in patterns


def _route_alerts(transfers, routes, scores_by_id):
    alerts = []
    for route in routes:
        for transfer in transfers:
            score = scores_by_id[transfer["id"]]
            if score["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], transfer["chain"]):
                continue
            if not _match(route["assets"], transfer["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "transfer_id": transfer["id"],
                    "severity": route["severity"],
                    "reason": list(score["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["transfer_id"]))
    return alerts


def analyze(payload):
    """对已解析的输入 JSON 执行完整分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    config = _validate_scoring(payload)

    graph = _build_graph(transfers)
    whales = [
        transfer["raw"]
        for transfer in sorted(
            (
                transfer
                for transfer in transfers
                if float(transfer["usd_value"]) >= threshold
            ),
            key=lambda transfer: transfer["id"],
        )
    ]
    scores = _score_transfers(transfers, threshold, config)
    scores_by_id = {item["id"]: item for item in scores}
    alerts = _route_alerts(transfers, routes, scores_by_id)

    return {
        "graph": graph,
        "whales": whales,
        "scores": scores,
        "alerts": alerts,
    }
