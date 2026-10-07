"""analyze 告警通知批次：``whale-radar dispatch`` 的核心逻辑。

输入为已解析的 JSON 对象（analyze 同形的 transfers、whale_threshold_usd、
routes 与可选 scoring，外加 dispatch_policy），输出 data 仅含 dispatches
数组；无候选告警时为空数组。

dispatch_policy 必须恰好含 dedupe_window_seconds（1..86400 整数，不接受
bool）与 escalate_score（0..100 有限数，不接受 bool）；缺失、含未知字段、
布尔或类型范围错误均报 INVALID_DISPATCH_POLICY。

候选沿用 analyze 的告警（route、score、reason 完全一致），按 target、
chain、asset 与转账两端地址（无序）分组；组内按解析时间、transfer_id
升序，首条为起点，闭区间到起点加 dedupe_window_seconds，窗外另起一批。
每批含 target、subject_addresses、chain、asset、transfer_ids、route_ids、
first_timestamp、last_timestamp、merged_count、severity、score、reason：
transfer_ids、route_ids、subject_addresses 去重升序，时间取首尾原
timestamp，merged_count 为合并的告警数，score 取最高，reason 按
VALUE、BURST、FAN_OUT、ROUND_TRIP 去重合并。severity 取 info、warning、
critical 最高者，score 达到 escalate_score 时改判 critical。dispatches
按 severity、score 降序，再按 target、transfer_ids 升序排序。

校验复用 analyzer，错误码与优先级为：

INPUT_NOT_JSON -> INVALID_INPUT_SCHEMA -> DUPLICATE_TRANSFER_ID
-> INVALID_TRANSFER_VALUE -> INVALID_THRESHOLD -> INVALID_ROUTE
-> INVALID_DISPATCH_POLICY -> INVALID_SCORING_CONFIG
"""

from __future__ import annotations

import math
from datetime import timedelta

from .analyzer import (
    REASON_BURST,
    REASON_FAN_OUT,
    REASON_ROUND_TRIP,
    REASON_VALUE,
    SEVERITIES,
    AnalyzeError,
    _is_number,
    _route_alerts,
    _score_transfers,
    _validate_duplicates,
    _validate_routes,
    _validate_schema,
    _validate_scoring,
    _validate_threshold,
    _validate_values,
)

DISPATCH_POLICY_FIELDS = ("dedupe_window_seconds", "escalate_score")

DEDUPE_WINDOW_MIN, DEDUPE_WINDOW_MAX = 1, 86400

REASON_ORDER = (REASON_VALUE, REASON_BURST, REASON_FAN_OUT, REASON_ROUND_TRIP)

_SEVERITY_RANK = {severity: index for index, severity in enumerate(SEVERITIES)}


def _validate_dispatch_policy(payload):
    """dispatch_policy 校验，返回 (dedupe_window_seconds, escalate_score)。

    必须恰好含两个已知字段：dedupe_window_seconds 为 1..86400 整数（不接受
    bool）、escalate_score 为 0..100 的有限数（不接受 bool）。任何缺失、
    未知字段、布尔或类型范围错误均抛 INVALID_DISPATCH_POLICY。
    """
    if "dispatch_policy" not in payload:
        raise AnalyzeError("INVALID_DISPATCH_POLICY")
    raw = payload["dispatch_policy"]
    if not isinstance(raw, dict):
        raise AnalyzeError("INVALID_DISPATCH_POLICY")
    if set(raw) != set(DISPATCH_POLICY_FIELDS):
        raise AnalyzeError("INVALID_DISPATCH_POLICY")

    dedupe_window_seconds = raw["dedupe_window_seconds"]
    escalate_score = raw["escalate_score"]

    if (
        not isinstance(dedupe_window_seconds, int)
        or isinstance(dedupe_window_seconds, bool)
        or not (DEDUPE_WINDOW_MIN <= dedupe_window_seconds <= DEDUPE_WINDOW_MAX)
    ):
        raise AnalyzeError("INVALID_DISPATCH_POLICY")
    if (
        not _is_number(escalate_score)
        or not math.isfinite(float(escalate_score))
        or not (0 <= float(escalate_score) <= 100)
    ):
        raise AnalyzeError("INVALID_DISPATCH_POLICY")

    return dedupe_window_seconds, float(escalate_score)


def _dispatch_candidates(transfers, threshold, config, routes):
    """沿用 analyze 的逐笔打分与 route 匹配生成候选告警。

    返回附解析时间与两端地址的候选列表，按 analyze 告警顺序
    （route_id、transfer_id）排列。
    """
    scores = _score_transfers(transfers, threshold, config)
    scores_by_id = {item["id"]: item for item in scores}
    alerts = _route_alerts(transfers, routes, scores_by_id)
    transfers_by_id = {transfer["id"]: transfer for transfer in transfers}
    candidates = []
    for alert in alerts:
        transfer = transfers_by_id[alert["transfer_id"]]
        candidates.append(
            {
                "route_id": alert["route_id"],
                "transfer_id": alert["transfer_id"],
                "severity": alert["severity"],
                "score": scores_by_id[alert["transfer_id"]]["score"],
                "reason": list(alert["reason"]),
                "target": alert["target"],
                "timestamp": transfer["timestamp"],
                "raw_timestamp": transfer["raw"]["timestamp"],
                "chain": transfer["chain"],
                "asset": transfer["asset"],
                "from_address": transfer["from_address"],
                "to_address": transfer["to_address"],
            }
        )
    return candidates


def _group_candidates(candidates):
    """按 (target, chain, asset, 两端地址无序集合) 分组。"""
    groups = {}
    for candidate in candidates:
        endpoints = frozenset(
            (candidate["from_address"], candidate["to_address"])
        )
        key = (candidate["target"], candidate["chain"], candidate["asset"],
               endpoints)
        groups.setdefault(key, []).append(candidate)
    return groups


def _batch_group(members, dedupe_window_seconds):
    """组内按解析时间、transfer_id 升序后贪心切闭窗口批次。

    首条为起点，纳入 timestamp<=起点+dedupe_window_seconds 的后续候选，
    窗外另起一批。
    """
    ordered = sorted(
        members,
        key=lambda candidate: (candidate["timestamp"],
                               candidate["transfer_id"]),
    )
    batches = []
    current = []
    window_end = None
    for candidate in ordered:
        if not current:
            current = [candidate]
            window_end = candidate["timestamp"] + timedelta(
                seconds=dedupe_window_seconds
            )
        elif candidate["timestamp"] <= window_end:
            current.append(candidate)
        else:
            batches.append(current)
            current = [candidate]
            window_end = candidate["timestamp"] + timedelta(
                seconds=dedupe_window_seconds
            )
    if current:
        batches.append(current)
    return batches


def _build_dispatch(key, members, dedupe_window_seconds, escalate_score):
    """把一个分组切成一批或多批通知并构造输出项。"""
    target, chain, asset, endpoints = key
    dispatches = []
    for batch in _batch_group(members, dedupe_window_seconds):
        score = max(candidate["score"] for candidate in batch)
        severity_rank = max(
            _SEVERITY_RANK[candidate["severity"]] for candidate in batch
        )
        if score >= escalate_score:
            severity_rank = _SEVERITY_RANK["critical"]

        merged_reasons = set()
        for candidate in batch:
            merged_reasons.update(candidate["reason"])
        reason = [name for name in REASON_ORDER if name in merged_reasons]

        dispatches.append(
            {
                "target": target,
                "subject_addresses": sorted(endpoints),
                "chain": chain,
                "asset": asset,
                "transfer_ids": sorted(
                    {candidate["transfer_id"] for candidate in batch}
                ),
                "route_ids": sorted(
                    {candidate["route_id"] for candidate in batch}
                ),
                "first_timestamp": batch[0]["raw_timestamp"],
                "last_timestamp": batch[-1]["raw_timestamp"],
                "merged_count": len(batch),
                "severity": SEVERITIES[severity_rank],
                "score": score,
                "reason": reason,
            }
        )
    return dispatches


def dispatch(payload):
    """对已解析的输入 JSON 执行告警通知批次合并，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    dedupe_window_seconds, escalate_score = _validate_dispatch_policy(payload)
    config = _validate_scoring(payload)

    candidates = _dispatch_candidates(
        transfers, threshold, config, routes
    )
    dispatches = []
    for key, members in _group_candidates(candidates).items():
        dispatches.extend(
            _build_dispatch(
                key, members, dedupe_window_seconds, escalate_score
            )
        )

    dispatches.sort(
        key=lambda item: (
            -_SEVERITY_RANK[item["severity"]],
            -float(item["score"]),
            item["target"],
            item["transfer_ids"],
        )
    )
    return {"dispatches": dispatches}
