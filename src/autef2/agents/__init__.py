"""The three agents that replace v1's single repair prompt.

    FailureAnalysisAgent  -- reads the traceback, names the root cause
    RepairStrategyAgent   -- maps that cause to a matching repair strategy
    AutoFixAgent          -- applies the repair, re-runs, and verifies

The orchestrator (``autef2.orchestrator``) wires them into a loop that
escalates to a different strategy when a repair does not hold.
"""

from .autofix import AutoFixAgent
from .failure_analysis import FailureAnalysisAgent, heuristic_classify
from .repair_strategy import RepairStrategyAgent

__all__ = [
    "AutoFixAgent",
    "FailureAnalysisAgent",
    "RepairStrategyAgent",
    "heuristic_classify",
]
