"""同链同资产转账多跳回流：``whale-radar cycles`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 cycle_query 查询对象），输出 data 仅含 cycles、
alerts 两个数组；无回路时两者均为空数组。

cycle_query 必须恰好含 max_hops（2..8 整数，不接受 bool）与 min_usd_value
（非负有限数，不接受 bool）；缺失、含未知字段或非法值均报
INVALID_CYCLE_QUERY。

cycle 为同一 (chain, asset) 组内 2..max_hops 跳的地址简单回路：路径上
除末节点与起点重复外，其余地址互不相同，自转账不能成环。不同边序列
（含反向回路）各自保留；nodes 从字典序最小参与地址起沿边回到该地址，
cycle_id 由 transfer_ids 以 ``>`` 连接。每项含 transfer_ids、cycle_id 及
nodes、hops、chain、asset、amount、usd_value、score、reason；金额与
score 保留 10 位小数，usd_value 小于 min_usd_value 的回路被过滤。score
为 analyze 逐段分值求和后截到 0..100，reason 按 VALUE、BURST、FAN_OUT、
ROUND_TRIP 顺序去重；cycles 按 score 降序、chain、asset、cycle_id 升序
排序。alerts 每个 route/cycle 至多一项，score>=route.min_score 且 chains、
assets 命中星号规则时生成，字段为 route_id、cycle_id、chain、asset、
severity、score、reason、target，按 route_id、cycle_id 排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_CYCLE_QUERY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math

from .analyzer import (
    AnalyzeError,
    _is_number,
    _match,
    _round10,
    _score_transfers,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)

CYCLE_QUERY_FIELDS = ("max_hops", "min_usd_value")

MAX_HOPS_MIN, MAX_HOPS_MAX = 2, 8

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _segments(segment_scores):
    """与 transfer_ids 同序的逐段归因：transfer_id、逐笔 score 与 reason。"""
    return [
        {
            "transfer_id": item["id"],
            "score": item["score"],
            "reason": list(item["reason"]),
        }
        for item in segment_scores
    ]


def _validate_cycle_query(payload):
    """cycle_query 校验，返回 (max_hops, min_usd_value)。

    必须恰好含两个已知字段：max_hops 为 2..8 整数（不接受 bool）、
    min_usd_value 为非负有限数（不接受 bool）。任何缺失、未知字段或
    非法值均抛 INVALID_CYCLE_QUERY。
    """
    if "cycle_query" not in payload:
        raise AnalyzeError("INVALID_CYCLE_QUERY")
    raw = payload["cycle_query"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_CYCLE_QUERY")
    if set(raw) != set(CYCLE_QUERY_FIELDS):
        raise AnalyzeError("INVALID_CYCLE_QUERY")

    max_hops = raw["max_hops"]
    min_usd_value = raw["min_usd_value"]

    if (
        not isinstance(max_hops, int)
        or isinstance(max_hops, bool)
        or not (MAX_HOPS_MIN <= max_hops <= MAX_HOPS_MAX)
    ):
        raise AnalyzeError("INVALID_CYCLE_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_CYCLE_QUERY")

    return max_hops, float(min_usd_value)


def _find_cycles(transfers, max_hops):
    """在每个 (chain, asset) 组内枚举全部地址简单回路（2..max_hops 跳）。

    仅从当前最小（字典序）地址起 DFS：节点集合中存在更小地址时跳过，
    因此每条无向简单回路只以其最小参与节点为起点；平行边（同端点不同
    transfer id）与反向遍历各自给出独立边序列，均保留。闭合边直接回到
    起点时记录，禁止回到路径上的其他地址，保证仅末节点重复起点。
    """
    groups = {}
    for transfer in transfers:
        groups.setdefault((transfer["chain"], transfer["asset"]), []).append(
            transfer
        )

    cycles = []
    for chain, asset in sorted(groups):
        edges = groups[(chain, asset)]
        adjacency = {}
        nodes = set()
        for transfer in edges:
            adjacency.setdefault(transfer["from_address"], []).append(transfer)
            nodes.add(transfer["from_address"])
            nodes.add(transfer["to_address"])

        def dfs(start, current, visited, node_path, transfer_ids,
                amount, usd_value):
            for edge in adjacency.get(current, ()):
                nxt = edge["to_address"]
                # 至少走过一条边后回到起点才构成回路；起点自转账不算。
                if nxt == start and transfer_ids:
                    cycles.append(
                        {
                            "nodes": list(node_path) + [start],
                            "transfer_ids": list(transfer_ids) + [edge["id"]],
                            "hops": len(transfer_ids) + 1,
                            "amount": amount + float(edge["amount"]),
                            "usd_value": usd_value + float(edge["usd_value"]),
                            "chain": chain,
                            "asset": asset,
                        }
                    )
                elif nxt not in visited and nxt > start:
                    if len(transfer_ids) + 1 >= max_hops:
                        continue
                    visited.add(nxt)
                    node_path.append(nxt)
                    transfer_ids.append(edge["id"])
                    dfs(
                        start,
                        nxt,
                        visited,
                        node_path,
                        transfer_ids,
                        amount + float(edge["amount"]),
                        usd_value + float(edge["usd_value"]),
                    )
                    transfer_ids.pop()
                    node_path.pop()
                    visited.discard(nxt)

        for start in sorted(nodes):
            dfs(start, start, {start}, [start], [], 0.0, 0.0)

    return cycles


def _build_cycles(transfers, threshold, config, max_hops, min_usd_value):
    """枚举回路并附加 analyze 逐段合并的分值、原因与 cycle_id，过滤排序。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    raw_cycles = _find_cycles(transfers, max_hops)

    cycles = []
    for cycle in raw_cycles:
        if cycle["usd_value"] < min_usd_value:
            continue
        segment_scores = [scores_by_id[tid] for tid in cycle["transfer_ids"]]
        total = sum(item["score"] for item in segment_scores)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [reason for reason in REASON_ORDER if reason in merged]

        cycles.append(
            {
                "transfer_ids": cycle["transfer_ids"],
                "cycle_id": ">".join(cycle["transfer_ids"]),
                "nodes": cycle["nodes"],
                "hops": cycle["hops"],
                "chain": cycle["chain"],
                "asset": cycle["asset"],
                "amount": _round10(cycle["amount"]),
                "usd_value": _round10(cycle["usd_value"]),
                "score": _round10(min(100.0, total)),
                "reason": reasons,
                "segments": _segments(segment_scores),
            }
        )

    cycles.sort(
        key=lambda item: (
            -item["score"],
            item["chain"],
            item["asset"],
            item["cycle_id"],
        )
    )
    return cycles


def _cycle_alerts(cycles, routes):
    """每条 route 与每条 cycle 至多一项告警，按 route_id、cycle_id 排序。"""
    alerts = []
    for route in routes:
        for cycle in cycles:
            if cycle["score"] < route["min_score"]:
                continue
            if not _match(route["chains"], cycle["chain"]):
                continue
            if not _match(route["assets"], cycle["asset"]):
                continue
            alerts.append(
                {
                    "route_id": route["id"],
                    "cycle_id": cycle["cycle_id"],
                    "chain": cycle["chain"],
                    "asset": cycle["asset"],
                    "severity": route["severity"],
                    "score": cycle["score"],
                    "reason": list(cycle["reason"]),
                    "target": route["target"],
                }
            )
    alerts.sort(key=lambda alert: (alert["route_id"], alert["cycle_id"]))
    return alerts


def cycles(payload):
    """对已解析的输入 JSON 执行多跳回流回路分析，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    max_hops, min_usd_value = _validate_cycle_query(payload)
    config = _validate_scoring(payload)

    cycle_list = _build_cycles(
        transfers, threshold, config, max_hops, min_usd_value
    )
    alerts = _cycle_alerts(cycle_list, routes)
    return {"cycles": cycle_list, "alerts": alerts}
