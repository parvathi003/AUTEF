"""Evaluation harness: does diagnosed repair actually beat one generic prompt?

The comparison is deliberately narrow. Both arms run on the *same* execution
stack -- pytest, traceback-based resolution, AST patching, the same isolated
environment -- so the only thing that differs is the repair architecture:

    baseline : one generic prompt, one attempt, no verification   (v1)
    autef2   : diagnose -> matched strategy -> verify -> escalate (v2)

Running v1's original executor as the baseline would confound the two claims:
a difference could then be explained by pytest collecting tests that unittest
never found, which says nothing about whether diagnosed repair is better. The
portability claim is measured separately, by how many projects each version can
process at all.

Each failing test is one observation, and both arms see byte-identical starting
state -- the project is restored from a pristine copy between arms.
"""

from .baseline import BaselineOrchestrator
from .benchmark import BenchmarkResult, ProjectSpec, run_benchmark
from .faults import FaultInjector, FaultRecord
from .metrics import ArmMetrics, compute_metrics, render_markdown

__all__ = [
    "ArmMetrics",
    "BaselineOrchestrator",
    "BenchmarkResult",
    "FaultInjector",
    "FaultRecord",
    "ProjectSpec",
    "compute_metrics",
    "render_markdown",
    "run_benchmark",
]
