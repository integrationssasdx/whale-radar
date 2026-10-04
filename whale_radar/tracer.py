"""资金路径追踪：在同链同资产转账构成的有向图上枚举简单路径。

输入为已解析的 JSON 对象，输出为可 JSON 序列化的 dict。
任何输入错误都抛出 ``TraceError(code)``，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_TRACE_QUERY

只在 chain 与 asset 与查询完全一致的转账中，沿 from_address -> to_address
枚举跳数不超过 max_hops 的所有简单路径（路径内地址不重复）。
"""

from __future__ import annotations

import math

from .analyzer import (
    TRANSFER_FIELDS,
    _is_number,
    _parse_timestamp,
    _round10,
)


class TraceError(Exception):
    """携带对外错误码的追踪异常。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


QUERY_FIELDS = ("chain", "asset", "start_address", "end_address", "max_hops")

MIN_HOPS = 1
MAX_HOPS_LIMIT = 8


def _validate_schema(payload):
    """结构与字段类型校验，返回 (transfers, 查询字段原始值所在 dict)。

    查询字段（chain、asset、start_address、end_address、max_hops）与
    transfers 同为顶层字段；其存在性与取值在转账全部校验之后再判定。
    """
    if not isinstance(payload, dict):
        raise TraceError("INVALID_INPUT_SCHEMA")

    if "transfers" not in payload or not isinstance(payload["transfers"], list):
        raise TraceError("INVALID_INPUT_SCHEMA")
    transfers = payload["transfers"]

    for transfer in transfers:
        if not isinstance(transfer, dict):
            raise TraceError("INVALID_INPUT_SCHEMA")
        for field in TRANSFER_FIELDS:
            if field not in transfer:
                raise TraceError("INVALID_INPUT_SCHEMA")
        for field in ("id", "chain", "asset", "from_address", "to_address"):
            value = transfer[field]
            if not isinstance(value, str) or not value:
                raise TraceError("INVALID_INPUT_SCHEMA")
        if not isinstance(transfer["timestamp"], str) or not transfer["timestamp"]:
            raise TraceError("INVALID_INPUT_SCHEMA")
        for field in ("amount", "usd_value"):
            if not _is_number(transfer[field]):
                raise TraceError("INVALID_INPUT_SCHEMA")

    return transfers, payload


def _validate_duplicates(transfers):
    seen = set()
    for transfer in transfers:
        transfer_id = transfer["id"]
        if transfer_id in seen:
            raise TraceError("DUPLICATE_TRANSFER_ID")
        seen.add(transfer_id)


def _validate_values(transfers):
    """时间与数值范围校验，返回精简后的标准化记录。"""
    normalized = []
    for transfer in transfers:
        if _parse_timestamp(transfer["timestamp"]) is None:
            raise TraceError("INVALID_TRANSFER_VALUE")
        amount = transfer["amount"]
        usd_value = transfer["usd_value"]
        if not math.isfinite(float(amount)) or amount <= 0:
            raise TraceError("INVALID_TRANSFER_VALUE")
        if not math.isfinite(float(usd_value)) or usd_value < 0:
            raise TraceError("INVALID_TRANSFER_VALUE")
        normalized.append(
            {
                "id": transfer["id"],
                "chain": transfer["chain"],
                "asset": transfer["asset"],
                "from_address": transfer["from_address"],
                "to_address": transfer["to_address"],
                "amount": float(amount),
                "usd_value": float(usd_value),
            }
        )
    return normalized


def _validate_query(query):
    """查询字段存在性、类型与取值范围校验，返回标准化查询。"""
    for field in QUERY_FIELDS:
        if field not in query:
            raise TraceError("INVALID_TRACE_QUERY")

    chain = query["chain"]
    asset = query["asset"]
    start = query["start_address"]
    end = query["end_address"]
    max_hops = query["max_hops"]

    for value in (chain, asset, start, end):
        if not isinstance(value, str) or not value:
            raise TraceError("INVALID_TRACE_QUERY")
    if start == end:
        raise TraceError("INVALID_TRACE_QUERY")

    # bool 是 int 的子类，必须排除。
    if not isinstance(max_hops, int) or isinstance(max_hops, bool):
        raise TraceError("INVALID_TRACE_QUERY")
    if not (MIN_HOPS <= max_hops <= MAX_HOPS_LIMIT):
        raise TraceError("INVALID_TRACE_QUERY")

    return {
        "chain": chain,
        "asset": asset,
        "start_address": start,
        "end_address": end,
        "max_hops": max_hops,
    }


def _build_adjacency(transfers, chain, asset):
    """同链同资产转账的邻接表：from -> [(to, id, amount, usd_value), ...]。"""
    adjacency = {}
    for transfer in transfers:
        if transfer["chain"] != chain or transfer["asset"] != asset:
            continue
        adjacency.setdefault(transfer["from_address"], []).append(
            (
                transfer["to_address"],
                transfer["id"],
                transfer["amount"],
                transfer["usd_value"],
            )
        )
    for edges in adjacency.values():
        # 稳定的邻接遍历顺序，保证路径枚举确定。
        edges.sort(key=lambda edge: (edge[0], edge[1]))
    return adjacency


def _enumerate_paths(adjacency, start, end, max_hops):
    """DFS 枚举从 start 到 end、跳数 1..max_hops 的全部简单路径。"""
    results = []

    def visit(node, visited, edges):
        if len(edges) >= max_hops:
            return
        for to_address, transfer_id, amount, usd_value in adjacency.get(node, []):
            if to_address in visited:
                continue
            next_edges = edges + [
                (transfer_id, to_address, amount, usd_value)
            ]
            if to_address == end:
                results.append(next_edges)
            else:
                visit(to_address, visited | {to_address}, next_edges)

    visit(start, {start}, [])
    return results


def _format_path(edges, start):
    nodes = [start]
    transfer_ids = []
    amount = 0.0
    usd_value = 0.0
    for transfer_id, to_address, edge_amount, edge_usd in edges:
        nodes.append(to_address)
        transfer_ids.append(transfer_id)
        amount += edge_amount
        usd_value += edge_usd
    return {
        "nodes": nodes,
        "transfer_ids": transfer_ids,
        "hops": len(edges),
        "amount": _round10(amount),
        "usd_value": _round10(usd_value),
    }


def trace(payload):
    """对已解析的输入 JSON 执行路径追踪，返回 data 载荷 dict。"""
    transfers_raw, query_raw = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    query = _validate_query(query_raw)

    adjacency = _build_adjacency(
        transfers, query["chain"], query["asset"]
    )
    edge_paths = _enumerate_paths(
        adjacency,
        query["start_address"],
        query["end_address"],
        query["max_hops"],
    )
    paths = [
        _format_path(edges, query["start_address"]) for edges in edge_paths
    ]
    paths.sort(key=lambda item: (item["hops"], item["transfer_ids"], item["nodes"]))
    return {"paths": paths}
