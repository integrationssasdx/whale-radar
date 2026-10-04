"""资金路径追踪：同链同资产转账上的简单路径搜索。

输入为已解析的 JSON 对象，输出为可 JSON 序列化的 dict。
任何输入错误都抛出 ``AnalyzeError(code)``，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_TRACE_QUERY
"""

from __future__ import annotations

from .analyzer import (
    AnalyzeError,
    _round10,
    _validate_duplicates,
    _validate_transfers,
    _validate_values,
)

MAX_HOPS_MIN = 1
MAX_HOPS_MAX = 8

QUERY_FIELDS = ("chain", "asset", "start_address", "end_address", "max_hops")


def _validate_query(payload):
    """查询字段校验，返回 (chain, asset, start, end, max_hops)。"""
    for field in QUERY_FIELDS:
        if field not in payload:
            raise AnalyzeError("INVALID_TRACE_QUERY")
    chain = payload["chain"]
    asset = payload["asset"]
    start = payload["start_address"]
    end = payload["end_address"]
    max_hops = payload["max_hops"]
    if not isinstance(chain, str) or not isinstance(asset, str):
        raise AnalyzeError("INVALID_TRACE_QUERY")
    if not isinstance(start, str) or not isinstance(end, str):
        raise AnalyzeError("INVALID_TRACE_QUERY")
    if not start or not end or start == end:
        raise AnalyzeError("INVALID_TRACE_QUERY")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise AnalyzeError("INVALID_TRACE_QUERY")
    if not (MAX_HOPS_MIN <= max_hops <= MAX_HOPS_MAX):
        raise AnalyzeError("INVALID_TRACE_QUERY")
    return chain, asset, start, end, max_hops


def _find_paths(transfers, chain, asset, start, end, max_hops):
    """DFS 枚举 start -> end 的全部简单路径（地址不重复，1..max_hops 跳）。"""
    adjacency = {}
    for transfer in transfers:
        if transfer["chain"] == chain and transfer["asset"] == asset:
            adjacency.setdefault(transfer["from_address"], []).append(transfer)

    paths = []

    def dfs(current, visited, transfer_ids, nodes, amount, usd_value):
        if current == end:
            paths.append(
                {
                    "nodes": list(nodes),
                    "transfer_ids": list(transfer_ids),
                    "hops": len(transfer_ids),
                    "amount": _round10(amount),
                    "usd_value": _round10(usd_value),
                }
            )
            # 终点不可重复出现在路径中，到达即完整，不再延伸。
            return
        if len(transfer_ids) >= max_hops:
            return
        for transfer in adjacency.get(current, ()):
            nxt = transfer["to_address"]
            if nxt in visited:
                continue
            visited.add(nxt)
            nodes.append(nxt)
            transfer_ids.append(transfer["id"])
            dfs(
                nxt,
                visited,
                transfer_ids,
                nodes,
                amount + float(transfer["amount"]),
                usd_value + float(transfer["usd_value"]),
            )
            transfer_ids.pop()
            nodes.pop()
            visited.discard(nxt)

    dfs(start, {start}, [], [start], 0.0, 0.0)
    paths.sort(key=lambda p: (p["hops"], p["transfer_ids"], p["nodes"]))
    return paths


def trace(payload):
    """对已解析的输入 JSON 执行路径追踪，返回 data 载荷 dict。"""
    if not isinstance(payload, dict) or "transfers" not in payload:
        raise AnalyzeError("INVALID_INPUT_SCHEMA")
    transfers_raw = payload["transfers"]
    _validate_transfers(transfers_raw)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    chain, asset, start, end, max_hops = _validate_query(payload)

    return {"paths": _find_paths(transfers, chain, asset, start, end, max_hops)}
