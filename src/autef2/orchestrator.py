"""The repair loop: diagnose, choose, apply, verify, escalate.

    for each failing test:
        signature -> cache?  yes: start from the strategy that worked before
                             no : Failure Analysis Agent names the root cause
        while attempts remain and the test still fails:
            Repair Strategy Agent picks the next *unused* strategy
            AutoFix Agent applies it and re-runs the test
            if it verified and did not weaken or regress -> done
            if the failure CHANGED, re-diagnose: the original cause is fixed
              and a different one is now in the way
            otherwise escalate to the next rung

The re-diagnosis branch is worth calling out. A repair that changes
ModuleNotFoundError into AssertionError did not fail -- it worked, and revealed
the next problem. Escalating the import ladder there would be wrong; what is
needed is a fresh diagnosis of the new failure. v1 could not make this
distinction because it never looked at the result of its own fix.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence

from .agents import AutoFixAgent, FailureAnalysisAgent, RepairStrategyAgent
from .cache import SignatureCache
from .config import AutefConfig
from .context import ContextBuilder
from .llm import LLMClient
from .models import (
    NON_REPAIRABLE,
    Diagnosis,
    ProjectLayout,
    RepairRecord,
    RootCause,
    RunReport,
    SuiteResult,
    TestFailure,
)
from .patcher import find_function
from .resolver import resolve_all
from .runner import TestRunner
from .strategies import strategy_by_id
from .venv_manager import Environment

logger = logging.getLogger(__name__)


class RepairOrchestrator:
    """Drives the three agents over every failing test in a project."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        llm: Optional[LLMClient],
        *,
        cache: Optional[SignatureCache] = None,
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.llm = llm

        self.runner = TestRunner(layout, environment, config)
        self.context = ContextBuilder(layout, config)
        self.cache = cache or SignatureCache(
            config.cache_path, enabled=config.use_signature_cache
        )

        self.analysis = FailureAnalysisAgent(llm, config)
        self.strategy = RepairStrategyAgent(llm, config)
        self.autofix = AutoFixAgent(llm, config, layout, self.runner, self.context)

    # -- entry point ------------------------------------------------------

    def run(self, *, max_tests: Optional[int] = None) -> RunReport:
        started = time.time()
        report = RunReport(project=self.layout.name, layout=self.layout, arm="autef2")

        logger.info("Running the suite for %s", self.layout.name)
        before = self.runner.run_suite()
        report.before = before

        if not before.ran:
            report.error = (
                "The test suite could not be executed. "
                + before.stdout_tail[-500:]
            )
            report.duration_s = time.time() - started
            return report

        failures = resolve_all(
            list(before.failures) + list(before.collection_errors), self.layout
        )
        if max_tests is not None:
            failures = failures[:max_tests]

        logger.info(
            "%d passing, %d failing to repair", len(before.passed), len(failures)
        )

        baseline_passing = list(before.passed)
        for index, failure in enumerate(failures, start=1):
            logger.info("[%d/%d] %s", index, len(failures), failure.nodeid)
            record = self.repair(failure, baseline_passing)
            report.records.append(record)

        logger.info("Re-running the full suite")
        report.after = self.runner.run_suite()

        if self.llm is not None:
            report.prompt_tokens = self.llm.usage.prompt_tokens
            report.completion_tokens = self.llm.usage.completion_tokens
            report.llm_calls = self.llm.usage.calls
            report.cost_usd = self.llm.usage.cost_usd
        report.duration_s = time.time() - started
        return report

    # -- per-test loop ----------------------------------------------------

    def diagnose(self, failure: TestFailure) -> RepairRecord:
        """Diagnose one failure without repairing it.

        Exists so a caller can present diagnosis and repair as separate steps
        -- the stagewise UI does -- while still going through exactly the code
        path ``repair`` uses. The returned record is handed straight back to
        ``repair``, which then skips re-diagnosing.
        """
        record = RepairRecord(nodeid=failure.nodeid, signature=failure.signature())
        diagnosis, _forced = self._initial_diagnosis(failure, record)
        record.diagnosis = diagnosis
        return record

    def repair(
        self,
        failure: TestFailure,
        baseline_passing: Sequence[str],
        *,
        record: Optional[RepairRecord] = None,
    ) -> RepairRecord:
        if record is not None and record.diagnosis is not None:
            diagnosis = record.diagnosis
            # A cache hit means the diagnosis came with a proven strategy; look
            # it up again rather than storing it on the record.
            forced_first = (
                self._cached_strategy(record.signature) if record.cache_hit else None
            )
        else:
            record = RepairRecord(
                nodeid=failure.nodeid, signature=failure.signature()
            )
            diagnosis, forced_first = self._initial_diagnosis(failure, record)
            record.diagnosis = diagnosis

        if diagnosis.root_cause in NON_REPAIRABLE:
            record.skipped_reason = (
                f"not repairable by editing the test: {diagnosis.root_cause.value}"
                f" -- {diagnosis.explanation}"
            )
            logger.info("  skipped: %s", record.skipped_reason)
            return record

        current = failure
        attempted: List[str] = []
        notes: List[str] = []

        while len(record.attempts) < self.config.max_attempts:
            if forced_first is not None and not attempted:
                strategy = forced_first
            else:
                strategy = self.strategy.select(diagnosis, current, attempted)

            if strategy is None:
                logger.info("  no untried strategy remains")
                break

            attempted.append(strategy.id)
            outcome = self.autofix.attempt(
                current,
                diagnosis,
                strategy,
                attempt_number=len(record.attempts) + 1,
                baseline_passing=baseline_passing,
                previous_notes=notes,
            )
            record.attempts.append(outcome.record)

            if outcome.accepted:
                record.fixed = True
                record.final_strategy_id = strategy.id
                self.cache.record_success(
                    record.signature,
                    diagnosis.root_cause,
                    strategy.id,
                    nodeid=failure.nodeid,
                    message=failure.exception_message,
                )
                return record

            if outcome.source_defect_claimed:
                record.skipped_reason = (
                    "the model reports the source is at fault; repairing the "
                    "test would hide a production defect"
                )
                return record

            notes.append(
                f"{strategy.label}: "
                f"{outcome.record.rejected_reason or outcome.record.new_failure or 'no effect'}"
            )

            # The repair may have moved the failure on rather than failing.
            if outcome.new_failure is not None:
                diagnosis, current = self._maybe_rediagnose(
                    current, outcome.new_failure, diagnosis, record
                )

        self.cache.record_failure(
            record.signature, attempted[-1] if attempted else ""
        )
        return record

    # -- diagnosis --------------------------------------------------------

    def _cached_strategy(self, signature: str):
        entry = self.cache.lookup(signature)
        return strategy_by_id(entry.strategy_id) if entry is not None else None

    def _initial_diagnosis(self, failure: TestFailure, record: RepairRecord):
        """Diagnose, or reuse a strategy proven against this signature."""
        entry = self.cache.lookup(record.signature)
        if entry is not None:
            strategy = strategy_by_id(entry.strategy_id)
            if strategy is not None:
                record.cache_hit = True
                cause = _cause_from_value(entry.root_cause)
                logger.info(
                    "  cache hit: %s previously fixed by %s (%d/%d)",
                    entry.root_cause, entry.strategy_id,
                    entry.successes, entry.successes + entry.failures,
                )
                return (
                    Diagnosis(
                        root_cause=cause,
                        confidence=entry.success_rate,
                        at_fault="test",
                        explanation=(
                            f"Reused from a previously repaired failure with the "
                            f"same signature (strategy {entry.strategy_id})."
                        ),
                        heuristic_cause=cause,
                    ),
                    strategy,
                )

        span = (
            find_function(failure.test_file, failure.test_function, failure.test_class)
            if failure.test_file and failure.test_function
            else None
        )
        context = self.context.build_diagnostic(failure, span)
        diagnosis = self.analysis.diagnose(failure, self.layout, context)
        logger.info(
            "  diagnosis: %s (%.2f, at fault: %s)",
            diagnosis.root_cause.value, diagnosis.confidence, diagnosis.at_fault,
        )
        return diagnosis, None

    def _maybe_rediagnose(
        self,
        current: TestFailure,
        new_failure: TestFailure,
        diagnosis: Diagnosis,
        record: RepairRecord,
    ):
        """Re-diagnose only when the failure genuinely changed shape."""
        if new_failure.signature() == current.signature():
            return diagnosis, current  # same problem; escalate the ladder

        logger.info(
            "  failure changed (%s -> %s); re-diagnosing",
            current.exception_type or "?", new_failure.exception_type or "?",
        )
        span = (
            find_function(
                new_failure.test_file, new_failure.test_function, new_failure.test_class
            )
            if new_failure.test_file and new_failure.test_function
            else None
        )
        context = self.context.build_diagnostic(new_failure, span)
        fresh = self.analysis.diagnose(new_failure, self.layout, context)
        logger.info(
            "  re-diagnosis: %s (%.2f)", fresh.root_cause.value, fresh.confidence
        )
        return fresh, new_failure


def _cause_from_value(value: str) -> RootCause:
    for cause in RootCause:
        if cause.value == value:
            return cause
    return RootCause.UNKNOWN
