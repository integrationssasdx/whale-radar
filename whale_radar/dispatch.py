"""告警通知批次：``whale-radar dispatch`` 的核心逻辑。

输入沿用 analyze 的 transfers、whale_threshold_usd、routes 与可选 scoring，
外加恰含 dedupe_window_seconds（1..86400 整数）与 escalate_score（0..100
有限数）的 ``dispatch_policy`` 对象；缺项、未知字段、布尔或类型范围错误均
报 INVALID_DISPATCH_POLICY。成功时 data 仅含 ``dispatches`` 数组。

候选为 analyze 在同一配置下生成的 route 告警，按 target、chain、asset 与
两端地址（无序）分组；组内按解析时间、transfer_id 升序，以首条为起点，
闭区间到起点 + dedupe_window_seconds，窗外另起一批（不回溯、不重叠）。

每批含 target、subject_addresses、chain、asset、transfer_ids、route_ids、
first_timestamp、last_timestamp、merged_count、severity、score、reason：
数组去重升序，时间取首尾原 timestamp，merged_count 为合并的告警数，score
取最高，reason 按 VALUE、BURST、FAN_OUT、ROUND_TRIP 去重合并；severity 取
info、warning、critical 的最高级，score 达到 escalate_score 时升为
critical。dispatches 按 severity、score 降序及 target、transfer_ids 升序。

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

REASON_ORDER = (
    REASON_VALUE,
    REASON_BURST,
    REASON_FAN_OUT,
    REASON_ROUND_TRIP,
)


def _validate_dispatch_policy(payload):
    """dispatch_policy 校验，返回 (dedupe_window_seconds, escalate_score)。

    必须恰好含两个已知字段：dedupe_window_seconds 为 1..86400 整数（不接受
    bool），escalate_score 为 0..100 的有限数（不接受 bool、NaN、Infinity）。
    任何缺失、未知字段或非法值均抛 INVALID_DISPATCH_POLICY。
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


def _group_alerts(alerts, transfers):
    """按 (target, chain, asset, 两端地址无序集合) 归组 analyze 告警。"""
    by_id = {transfer["id"]: transfer for transfer in transfers}
    groups = {}
    for alert in alerts:
        transfer = by_id[alert["transfer_id"]]
        endpoints = frozenset(
            (transfer["from_address"], transfer["to_address"])
        )
        key = (alert["target"], transfer["chain"], transfer["asset"], endpoints)
        groups.setdefault(key, []).append((alert, transfer))
    return groups


def _split_batches(members, dedupe_window_seconds):
    """组内按解析时间、transfer_id 升序后做闭窗口贪心切批。

    以批次首条为起点，纳入 timestamp <= 起点 + dedupe_window_seconds 的
    后续成员，窗外成员另起一批；窗口为闭区间，批次不重叠。
    """
    ordered = sorted(
        members,
        key=lambda item: (item[1]["timestamp"], item[0]["transfer_id"]),
    )
    batches = []
    current = []
    window_end = None
    for member in ordered:
        timestamp = member[1]["timestamp"]
        if not current:
            current.append(member)
            window_end = timestamp + timedelta(
                seconds=dedupe_window_seconds
            )
        elif timestamp <= window_end:
            current.append(member)
        else:
            batches.append(current)
            current = [member]
            window_end = timestamp + timedelta(
                seconds=dedupe_window_seconds
            )
    if current:
        batches.append(current)
    return batches


def _build_dispatch(key, batch, scores_by_id, escalate_score):
    target, chain, asset, endpoints = key
    alerts = [alert for alert, _transfer in batch]

    transfer_ids = sorted({alert["transfer_id"] for alert in alerts})
    route_ids = sorted({alert["route_id"] for alert in alerts})

    score = max(
        scores_by_id[alert["transfer_id"]]["score"] for alert in alerts
    )
    severity_rank = max(
        SEVERITIES.index(alert["severity"]) for alert in alerts
    )
    if score >= escalate_score:
        severity_rank = SEVERITIES.index("critical")

    reasons = set()
    for alert in alerts:
        reasons.update(alert["reason"])
    reason = [name for name in REASON_ORDER if name in reasons]

    return {
        "target": target,
        "subject_addresses": sorted(endpoints),
        "chain": chain,
        "asset": asset,
        "transfer_ids": transfer_ids,
        "route_ids": route_ids,
        "first_timestamp": batch[0][1]["raw"]["timestamp"],
        "last_timestamp": batch[-1][1]["raw"]["timestamp"],
        "merged_count": len(batch),
        "severity": SEVERITIES[severity_rank],
        "score": score,
        "reason": reason,
    }


def dispatch(payload):
    """对已解析的输入 JSON 生成告警通知批次，返回 data 载荷 dict。"""
    transfers_raw, threshold, routes = _validate_schema(payload)
    _validate_duplicates(transfers_raw)
    transfers = _validate_values(transfers_raw)
    _validate_threshold(threshold)
    _validate_routes(routes)
    dedupe_window_seconds, escalate_score = _validate_dispatch_policy(payload)
    config = _validate_scoring(payload)

    scores = _score_transfers(transfers, threshold, config)
    scores_by_id = {item["id"]: item for item in scores}
    alerts = _route_alerts(transfers, routes, scores_by_id)

    groups = _group_alerts(alerts, transfers)
    dispatches = []
    for key, members in groups.items():
        for batch in _split_batches(members, dedupe_window_seconds):
            dispatches.append(
                _build_dispatch(key, batch, scores_by_id, escalate_score)
            )

    dispatches.sort(
        key=lambda item: (
            -SEVERITIES.index(item["severity"]),
            -item["score"],
            item["target"],
            item["transfer_ids"],
        )
    )
    return {"dispatches": dispatches}
