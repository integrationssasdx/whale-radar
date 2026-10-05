"""同链同资产多跳回流回路：``whale-radar cycles`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes，可选 scoring，外加 cycle_query 对象），输出 data 仅含 cycles、
alerts 两个数组；无回路时两者均为空数组。

cycle_query 必须恰好含 max_hops（2..8 整数）与 min_usd_value（非负有限
数）；缺失、含未知字段、布尔或非法值均报 INVALID_CYCLE_QUERY。

回路在每个 (chain, asset) 组内枚举：2..max_hops 跳的地址简单回路，仅末
节点重复起点，不同边序列（含反向）均保留；nodes 从字典序最小的参与地址
起并回到它，cycle_id 由 transfer_ids 以 ``>`` 连接。每项含 transfer_ids、
cycle_id 及 hops、chain、asset、amount、usd_value、score、reason；金额
与分值保留 10 位小数，usd_value 小于 min_usd_value 的回路被过滤。逐段
分值与原因沿用 analyze 的逐笔打分，回路分值为各段之和后截到 0..100，
原因按 VALUE、BURST、FAN_OUT、ROUND_TRIP 顺序去重。cycles 按 score
降序，再按 chain、asset、cycle_id 升序排序。

alerts 每个 route/cycle 至多一项，score>=route.min_score 且 chains、
assets 命中星号规则时生成，每项含 route_id、cycle_id、chain、asset、
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
from .tracer import MAX_HOPS_MAX

CYCLE_QUERY_FIELDS = ("max_hops", "min_usd_value")

MAX_HOPS_MIN = 2

REASON_ORDER = ("VALUE", "BURST", "FAN_OUT", "ROUND_TRIP")


def _validate_cycle_query(payload):
    """cycle_query 校验，返回 (max_hops, min_usd_value)。

    必须恰好含两个已知字段：max_hops 为 2..8 的整数（不接受 bool），
    min_usd_value 为非负有限数（不接受 bool）。缺失、未知字段或非法值
    均抛 INVALID_CYCLE_QUERY。
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

    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise AnalyzeError("INVALID_CYCLE_QUERY")
    if not (MAX_HOPS_MIN <= max_hops <= MAX_HOPS_MAX):
        raise AnalyzeError("INVALID_CYCLE_QUERY")
    if (
        not _is_number(min_usd_value)
        or not math.isfinite(float(min_usd_value))
        or min_usd_value < 0
    ):
        raise AnalyzeError("INVALID_CYCLE_QUERY")

    return max_hops, float(min_usd_value)


def _find_cycles(transfers, max_hops):
    """在每个 (chain, asset) 组内枚举全部地址简单回路。

    回路为 2..max_hops 跳、仅末节点重复起点的闭合行走；以每个参与地址为
    起点 DFS，且只允许经由字典序不小于起点的地址，使每条几何回路恰好在其
    字典序最小参与地址处生成一次（同一起点上的不同边序列仍各自保留）。
    """
    groups = {}
    for transfer in transfers:
        groups.setdefault((transfer["chain"], transfer["asset"]), []).append(
            transfer
        )

    cycles = []
    for chain, asset in sorted(groups):
        adjacency = {}
        addresses = set()
        for transfer in groups[(chain, asset)]:
            adjacency.setdefault(transfer["from_address"], []).append(transfer)
            addresses.add(transfer["from_address"])
            addresses.add(transfer["to_address"])

        def dfs(start, current, visited, nodes, transfer_ids, amount,
                usd_value):
            # 闭合边在本次调用中追加，故已有边数达到 max_hops 时不再查看
            # 出边：回路长度恰为 2..max_hops。
            if len(transfer_ids) >= max_hops:
                return
            hops = len(transfer_ids)
            for edge in adjacency.get(current, ()):
                nxt = edge["to_address"]
                if nxt == start:
                    # 仅末节点重复起点；自转账（1 跳）不计入回路。
                    if hops + 1 >= MAX_HOPS_MIN:
                        cycles.append(
                            {
                                "nodes": list(nodes) + [start],
                                "transfer_ids": list(transfer_ids)
                                + [edge["id"]],
                                "hops": hops + 1,
                                "amount": amount + float(edge["amount"]),
                                "usd_value": usd_value
                                + float(edge["usd_value"]),
                                "chain": chain,
                                "asset": asset,
                            }
                        )
                    continue
                # 中途节点必须严格大于起点：起点保留给闭合边，且每个几何
                # 回路只在其字典序最小参与地址处生成一次。
                if nxt <= start or nxt in visited:
                    continue
                visited.add(nxt)
                nodes.append(nxt)
                transfer_ids.append(edge["id"])
                dfs(
                    start,
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

        for start in sorted(addresses):
            dfs(start, start, {start}, [start], [], 0.0, 0.0)

    return cycles


def _cycles(transfers, threshold, config, max_hops, min_usd_value):
    """为拓扑回路附加 analyze 逐段合并的分值、原因与 cycle_id 等字段。"""
    scores_by_id = {
        item["id"]: item
        for item in _score_transfers(transfers, threshold, config)
    }
    raw_cycles = _find_cycles(transfers, max_hops)

    cycles = []
    for cycle in raw_cycles:
        usd_value = _round10(cycle["usd_value"])
        if usd_value < min_usd_value:
            continue

        segment_scores = [scores_by_id[tid] for tid in cycle["transfer_ids"]]
        total = sum(item["score"] for item in segment_scores)

        merged = set()
        for item in segment_scores:
            merged.update(item["reason"])
        reasons = [reason for reason in REASON_ORDER if reason in merged]

        transfer_ids = cycle["transfer_ids"]
        cycles.append(
            {
                "nodes": cycle["nodes"],
                "transfer_ids": transfer_ids,
                "cycle_id": ">".join(transfer_ids),
                "hops": cycle["hops"],
                "chain": cycle["chain"],
                "asset": cycle["asset"],
                "amount": _round10(cycle["amount"]),
                "usd_value": usd_value,
                "score": _round10(max(0.0, min(100.0, total))),
                "reason": reasons,
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

    result = _cycles(
        transfers, threshold, config, max_hops, min_usd_value
    )
    alerts = _cycle_alerts(result, routes)
    return {"cycles": result, "alerts": alerts}
