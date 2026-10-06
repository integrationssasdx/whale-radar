"""共同地址相连的转账网络（cluster）：``whale-radar cluster`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，外加 cluster_query 查询对象），输出 data 仅含 clusters、alerts
两个数组；无网络时两者均为空数组。

cluster_query 必须恰好含 window_start、window_end、min_usd_value：
window_start 与 window_end 为带时区的 RFC3339 字符串且结束晚于开始，
min_usd_value 为非负有限数（不接受布尔值）；缺失、含未知字段、布尔或
非法值均报 INVALID_CLUSTER_QUERY。

转账先按 [window_start, window_end] 闭区间过滤，再按 (chain, asset)
分组并排除自转账；组内由共同地址相连的转账归入同一网络。网络须至少两笔、
至少两个地址，usd_value 为纳入转账之和且不低于 min_usd_value。每项含
cluster_id、chain、asset、transfer_ids、usd_value、score；transfer_ids
按 timestamp、id 排序并以 ``>`` 连接为 cluster_id，usd_value 与 score
保留 10 位小数。score=min(100, 40*usd_value/whale_threshold_usd
+5*len(transfer_ids))。clusters 按 score 降序，再按 chain、asset、
cluster_id 升序。alerts 每个 route/cluster 至多一项，score>=route.min_score
且 chains、assets 命中星号规则时生成，每项含 route_id、cluster_id、
severity、score、target，按 route_id、cluster_id 排序。

cluster 不做打分配置，scoring（即使字段未知或非法）对其完全忽略。
校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_CLUSTER_QUERY
"""

from __future__ import annotations

import math

from .analyzer import (
    AnalyzeError,
    _is_number,
    _match,
    _parse_timestamp,
    _round10,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_threshold,
    _validate_values,
)

CLUSTER_QUERY_FIELDS = ("window_start", "window_end", "min_usd_value")


def _validate_cluster_query(payload):
    """cluster_query 校验，返回 (window_start, window_end, min_usd_value)。

    必须恰好含三个已知字段：window_start 与 window_end 为带时区的
    RFC3339 字符串且 window_end 严格晚于 window_start，min_usd_value 为
    非负有限数（不接受 bool）。任何缺失、未知字段或非法值均抛
    INVALID_CLUSTER_QUERY。
    """
    if "cluster_query" not in payload:
        raise AnalyzeError("INVALID_CLUSTER_QUERY")
    raw = payload["cluster_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")
    if set(raw) != set(CLUSTER_QUERY_FIELDS):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")

    window_start = _parse_timestamp(raw["window_start"])
    window_end = _parse_timestamp(raw["window_end"])
    if (
        window_start is None
        or window_end is None
        or window_end <= window_start
    ):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")

    min_usd_value = raw["min_usd_value"]
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")

    return window_start, window_end, float(min_usd_value)


def _clusters(transfers, threshold, window_start, window_end, min_usd_value):
    """按 (chain, asset) 分组，以共同地址连通分量枚举网络并附加分值。"""
    groups = {}
    for transfer in transfers:
        if transfer["from_address"] == transfer["to_address"]:
            continue
        if not (window_start <= transfer["timestamp"] <= window_end):
            continue
        key = (transfer["chain"], transfer["asset"])
        groups.setdefault(key, []).append(transfer)

    clusters = []
    for chain, asset in sorted(groups):
        edges = groups[(chain, asset)]

        parent = {}

        def find(address):
            root = address
            while parent[root] != root:
                root = parent[root]
            while parent[address] != root:
                parent[address], address = root, parent[address]
            return root

        for transfer in edges:
            parent.setdefault(transfer["from_address"], transfer["from_address"])
            parent.setdefault(transfer["to_address"], transfer["to_address"])
        for transfer in edges:
            root_from = find(transfer["from_address"])
            root_to = find(transfer["to_address"])
            if root_from != root_to:
                parent[root_to] = root_from

        components = {}
        for transfer in edges:
            components.setdefault(find(transfer["from_address"]), []).append(
                transfer
            )

        for members in components.values():
            addresses = set()
            for transfer in members:
                addresses.add(transfer["from_address"])
                addresses.add(transfer["to_address"])
            if len(members) < 2 or len(addresses) < 2:
                continue
            usd_value = sum(float(transfer["usd_value"]) for transfer in members)
            if usd_value < min_usd_value:
                continue

            members = sorted(
                members,
                key=lambda transfer: (transfer["timestamp"], transfer["id"]),
            )
            transfer_ids = [transfer["id"] for transfer in members]
            score = min(
                100.0,
                40.0 * usd_value / threshold + 5.0 * len(transfer_ids),
            )
            clusters.append(
                {
                    "cluster_id": ">".join(transfer_ids),
                    "chain": chain,
                    "asset": asset,
                    "transfer_ids": transfer_ids,
                    "usd_value": _round10(usd_value),
                    "score": _round10(score),
                }
            )

    clusters.sort(
        key=lambda cluster: (
            -cluster["score"],
            cluster["chain"],
            cluster["asset"],
            cluster["cluster_id"],
        )
    )
    return clusters


def _cluster_alerts(clusters, routes):
    """每条 route 与每个 cluster 至多一项告警，按 route_id、cluster_id 排序。"""
    alerts = []
    for route in routes:
        for cluster in clusters:
            if cluster["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], cluster["chain"]):
                continue
            if not _match(route["assets"], cluster["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "cluster_id": cluster["cluster_id"],
                    "severity": route["severity"],
                    "score": cluster["score"],
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["cluster_id"]))
    return alerts


def cluster(payload):
    """对已解析的输入 JSON 执行转账网络聚类分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    window_start, window_end, min_usd_value = _validate_cluster_query(payload)

    clusters = _clusters(
        transfers, threshold, window_start, window_end, min_usd_value
    )
    alerts = _cluster_alerts(clusters, routes)
    return {"clusters": clusters, "alerts": alerts}
