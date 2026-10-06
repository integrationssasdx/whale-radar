"""关注地址协同资金流：``whale-radar watch`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 watch_addresses 与 max_hops），输出 data 仅含
paths、alerts 两个数组；无路径时两者均为空数组。

paths 在每个 (chain, asset) 组内枚举关注地址之间的有向简单路径（地址不
重复，1..max_hops 跳，起止均为关注地址，中途可经过其他关注地址）；逐段
分值与原因沿用 analyze 的逐笔打分，路径分值为各段之和后截到 0..100，原因
按 VALUE BURST FAN_OUT ROUND_TRIP 顺序去重。每条路径另附 segments：
与 transfer_ids 同序对应，每项恰含 transfer_id、score（analyze 同一
scoring 配置下的逐笔 0..100 分，保留 10 位小数）、reason（按
VALUE BURST FAN_OUT ROUND_TRIP 去重）。paths 按 score 降序，再按
hops、chain、asset、transfer_ids 升序排序。alerts 每个 route/path 至多
一项，需 score>=route.min_score 且 chains、assets 命中星号规则。

校验复用 analyzer 与 tracer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_WATCH_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

from .analyzer import (
    AnalyzeError,
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
from .tracer import MAX_HOPS_MAX, MAX_HOPS_MIN

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _validate_watch_query(payload):
    """watch_addresses/max_hops 校验，返回 (watch_addresses, max_hops)。

    watch_addresses 必须是至少两个互异非空字符串的列表；max_hops 必须是
    1..8 的整数（不接受 bool）。任何不合法均抛 INVALID_WATCH_QUERY。
    """
    if "watch_addresses" not in payload or "max_hops" not in payload:
        raise AnalyzeError("INVALID_WATCH_QUERY")
    addresses = payload["watch_addresses"]
    max_hops = payload["max_hops"]

    if not isinstance(addresses, list) or len(addresses) < 2:
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if not all(isinstance(address, str) and address for address in addresses):
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if len(set(addresses)) != len(addresses):
        raise AnalyzeError("INVALID_WATCH_QUERY")

    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise AnalyzeError("INVALID_WATCH_QUERY")
    if not (MAX_HOPS_MIN <= max_hops <= MAX_HOPS_MAX):
        raise AnalyzeError("INVALID_WATCH_QUERY")

    return addresses, max_hops


def _find_watch_paths(transfers, watch_set, max_hops):
    """在每个 (chain, asset) 组内 DFS 枚举关注地址间的全部简单路径。

    与 tracer 不同：终点不是单一地址，DFS 经过任何其他关注地址即记录一条
    路径，且仍可继续延伸（中途允许出现其他关注地址），直至 max_hops 跳。
    """
    groups = {}
    for transfer in transfers:
        groups.setdefault((transfer["chain"], transfer["asset"]), []).append(
            transfer
        )

    paths = []
    for chain, asset in sorted(groups):
        adjacency = {}
        for transfer in groups[(chain, asset)]:
            adjacency.setdefault(transfer["from_address"], []).append(transfer)

        def dfs(current, visited, nodes, transfer_ids, amount, usd_value):
            if current in watch_set and len(nodes) > 1:
                paths.append(
                    {
                        "nodes": list(nodes),
                        "transfer_ids": list(transfer_ids),
                        "hops": len(transfer_ids),
                        "amount": amount,
                        "usd_value": usd_value,
                        "chain": chain,
                        "asset": asset,
                    }
                )
            if len(transfer_ids) >= max_hops:
                return
            for edge in adjacency.get(current, ()):
                nxt = edge["to_address"]
                if nxt in visited:
                    continue
                visited.add(nxt)
                nodes.append(nxt)
                transfer_ids.append(edge["id"])
                dfs(
                    nxt,
                    visited,
                    nodes,
                    transfer_ids,
                    amount + float(edge["amount"]),
                    usd_value + float(edge["usd_value"]),
                )
                transfer_ids.pop()
                nodes.pop()
                visited.discard(nxt)

        for start in sorted(watch_set):
            dfs(start, {start}, [start], [], 0.0, 0.0)

    return paths


def _watch_paths(transfers, threshold, config, watch_addresses, max_hops):
    """为拓扑路径附加 analyze 逐段合并的分值、原因与 path_id 等字段。"""
    watch_set = set(watch_addresses)
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    raw_paths = _find_watch_paths(transfers, watch_set, max_hops)

    paths = []
    for path in raw_paths:
        segment_scores = [scores_by_id[tid] for tid in path["transfer_ids"]]
        total = sum(item["score"] for item in segment_scores)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [reason for reason in REASON_ORDER if reason in merged]

        nodes = path["nodes"]
        paths.append(
            {
                "nodes": nodes,
                "transfer_ids": path["transfer_ids"],
                "hops": path["hops"],
                "amount": _round10(path["amount"]),
                "usd_value": _round10(path["usd_value"]),
                "from_address": nodes[0],
                "to_address": nodes[-1],
                "chain": path["chain"],
                "asset": path["asset"],
                "path_id": ">".join(path["transfer_ids"]),
                "score": _round10(min(100.0, total)),
                "reason": reasons,
                "segments": _segments(segment_scores),
            }
        )

    paths.sort(
        key=lambda item: (
            -item["score"],
            item["hops"],
            item["chain"],
            item["asset"],
            item["transfer_ids"],
        )
    )
    return paths


def _watch_alerts(paths, routes):
    """每条 route 与每条 path 至多一项告警，按 route_id、path_id 排序。"""
    alerts = []
    for route in routes:
        for path in paths:
            if path["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], path["chain"]):
                continue
            if not _match(route["assets"], path["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "path_id": path["path_id"],
                    "from_address": path["from_address"],
                    "to_address": path["to_address"],
                    "chain": path["chain"],
                    "asset": path["asset"],
                    "severity": route["severity"],
                    "score": path["score"],
                    "reason": list(path["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["path_id"]))
    return alerts


def watch(payload):
    """对已解析的输入 JSON 执行关注地址协同资金流分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    watch_addresses, max_hops = _validate_watch_query(payload)
    config = _validate_scoring(payload)

    paths = _watch_paths(
        transfers, threshold, config, watch_addresses, max_hops
    )
    alerts = _watch_alerts(paths, routes)
    return {"paths": paths, "alerts": alerts}
