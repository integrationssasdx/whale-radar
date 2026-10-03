"""Whale Radar：链上异常与巨鲸追踪、资金流图、异常打分与告警路由。"""

__version__ = "0.1.0"

__all__ = ["analyze", "AnalyzeError"]

from .analyzer import AnalyzeError, analyze
