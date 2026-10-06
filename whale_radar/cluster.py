"""窗口内同链同资产的巨鲸资金网络：``whale-radar cluster`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes），外加 ``cluster_query`` 查询对象，输出 data 仅含 clusters、alerts
两个数组；无网络时两者均为空数组。

cluster_query 必须恰好含 window_start、window_end（均为带时区的 RFC3339
字符串，且 window_end 晚于 window_start）与 min_usd_value（非负有限数，
不接受 bool）；缺失、含未知字段、布尔或类型/范围错误均报
INVALID_CLUSTER_QUERY。

在 window_start 到 window_end 的闭区间内，转账按 (chain, asset) 分组并
排除自转账；以地址为节点、转账为无向边做连通分量，共同地址相连的转账归入
同一网络。网络须至少两笔转账、至少两个地址，usd_value 为纳入转账之和且
不低于 min_usd_value（等值保留）。cluster_id 由 transfer_ids 以 ``>``
连接，transfer_ids 按 timestamp、id 升序；
score=min(100,40*usd_value/whale_threshold_usd+5*len(transfer_ids))，
score 与 usd_value 保留 10 位小数。clusters 按 score 降序，再按 chain、
asset、cluster_id 升序排序。alerts 每个 route/cluster 至多一项，score>=
route.min_score 且 chains、assets 命中星号规则时生成，每项含 route_id、
cluster_id、severity、score、target，按 route_id、cluster_id 排序。

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

    必须恰好含三个已知字段：window_start、window_end 均为带时区的 RFC3339
    字符串，且结束晚于开始；min_usd_value 为非负有限数（不接受 bool）。
    任何缺失、未知字段、布尔或类型/范围错误均抛 INVALID_CLUSTER_QUERY。
    """
    if "cluster_query" not in payload:
        raise AnalyzeError("INVALID_CLUSTER_QUERY")
    raw = payload["cluster_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")
    if set(raw) != set(CLUSTER_QUERY_FIELDS):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")

    window_start = raw["window_start"]
    window_end = raw["window_end"]
    min_usd_value = raw["min_usd_value"]

    start = _parse_timestamp(window_start)
    end = _parse_timestamp(window_end)
    if start is None or end is None or not end > start:
        raise AnalyzeError("INVALID_CLUSTER_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_CLUSTER_QUERY")

    return start, end, float(min_usd_value)


def _connected_components(transfers):
    """以地址为节点、转账为无向边求连通分量，返回每个分量的转账列表。

    共同地址相连的转账归入同一网络；用并查集合并每笔转账的两端地址，
    再按转账发起端的根归组。
    """
    parent = {}

    def find(address):
        parent.setdefault(address, address)
        root = address
        while parent[root] != root:
            root = parent[root]
        while parent[address] != root:
            parent[address], address = root, parent[address]
        return root

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for transfer in transfers:
        union(transfer["from_address"], transfer["to_address"])

    groups = {}
    for transfer in transfers:
        groups.setdefault(find(transfer["from_address"]), []).append(transfer)
    return list(groups.values())


def _build_clusters(transfers, threshold, start, end, min_usd_value):
    """筛选窗口内转账，分组求连通网络并打分、过滤、排序。"""
    groups = {}
    for transfer in transfers:
        if transfer["from_address"] == transfer["to_address"]:
            continue
        if not (start <= transfer["timestamp"] <= end):
            continue
        groups.setdefault((transfer["chain"], transfer["asset"]), []).append(
            transfer
        )

    clusters = []
    for chain, asset in sorted(groups):
        for component in _connected_components(groups[(chain, asset)]):
            ordered = sorted(
                component,
                key=lambda transfer: (transfer["timestamp"], transfer["id"]),
            )
            if len(ordered) < 2:
                continue
            addresses = set()
            for transfer in ordered:
                addresses.add(transfer["from_address"])
                addresses.add(transfer["to_address"])
            if len(addresses) < 2:
                continue

            usd_value = sum(float(t["usd_value"]) for t in ordered)
            if usd_value < min_usd_value:
                continue

            transfer_ids = [transfer["id"] for transfer in ordered]
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
        key=lambda item: (
            -item["score"],
            item["chain"],
            item["asset"],
            item["cluster_id"],
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
    """对已解析的输入 JSON 执行巨鲸资金网络分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    start, end, min_usd_value = _validate_cluster_query(payload)

    cluster_list = _build_clusters(
        transfers, threshold, start, end, min_usd_value
    )
    alerts = _cluster_alerts(cluster_list, routes)
    return {"clusters": cluster_list, "alerts": alerts}
