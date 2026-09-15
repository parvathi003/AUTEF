"""End-to-end: an archive or directory in, a RunReport out.

    ingest -> prepare environment -> run suite -> repair each failure -> re-run

Every stage is portable. Nothing in this path knows the name of any particular
application, and the only paths involved come from the workspace or from the
uploaded project itself.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import IO, Optional, Union

from .cache import SignatureCache
from .config import AutefConfig
from .ingest import IngestError, ingest
from .llm import LLMClient, LLMError
from .models import ProjectLayout, RunReport
from .orchestrator import RepairOrchestrator
from .venv_manager import Environment, prepare_environment

logger = logging.getLogger(__name__)

Source = Union[str, Path, IO[bytes]]


def run_project(
    source: Source,
    config: Optional[AutefConfig] = None,
    *,
    name_hint: Optional[str] = None,
    max_tests: Optional[int] = None,
    llm: Optional[LLMClient] = None,
    cache: Optional[SignatureCache] = None,
    dry_run: bool = False,
    enhance: Optional["EnhanceOptions"] = None,
) -> RunReport:
    """Ingest a project, run its tests, and improve them.

    The order is v1's, on v2's stack:

        generate (if asked, or if there are no tests)
        -> repair what fails
        -> raise coverage, repair what that broke
        -> raise mutation score

    ``enhance`` selects which phases run; with none of them the behaviour is
    exactly the repair-only pipeline. ``dry_run`` stops before any model call,
    which is how a project is checked for scope without spending anything.
    """
    from .enhance import EnhanceOptions

    config = config or AutefConfig.from_env()
    options = enhance if enhance is not None else EnhanceOptions()
    started = time.time()

    try:
        layout = ingest(source, config, name_hint=name_hint)
    except IngestError as exc:
        report = RunReport(project=str(name_hint or source), error=str(exc))
        report.duration_s = time.time() - started
        return report

    environment = prepare_environment(layout, config)
    for warning in environment.warnings:
        logger.warning("environment: %s", warning)

    if dry_run:
        return _dry_run_report(layout, environment, config, started)

    if llm is None:
        try:
            llm = LLMClient(config)
        except LLMError as exc:
            report = RunReport(project=layout.name, layout=layout, error=str(exc))
            report.duration_s = time.time() - started
            return report

    generation = None
    if options.generate or (options.generate_if_empty and not layout.test_files):
        from .enhance import GenerationPhase

        if not layout.test_files:
            logger.info("The project ships no tests; generating a suite first")
        generation = GenerationPhase(layout, environment, config, llm).run(
            max_modules=options.max_modules
        )
        layout = generation.layout or layout

    orchestrator = RepairOrchestrator(layout, environment, config, llm, cache=cache)
    report = orchestrator.run(max_tests=max_tests)
    layout = orchestrator.layout
    if generation is not None:
        report.generated = generation.records

    if options.coverage:
        from .enhance import CoveragePhase

        phase = CoveragePhase(layout, environment, config, llm)
        outcome = phase.run(max_files=options.max_coverage_files)
        report.coverage_before = outcome.before
        report.coverage_after = outcome.after
        report.coverage_generated = outcome.records
        layout = outcome.layout or layout
        if outcome.accepted:
            # Coverage tests can fail like any others; that is the repair loop's
            # job, and leaving them failing would make the run look worse than
            # the pipeline actually is.
            _repair_again(layout, environment, config, llm, cache, report, max_tests)

    if options.mutation:
        from .enhance import MutationPhase

        phase = MutationPhase(layout, environment, config, llm)
        outcome = phase.run(
            max_mutants=options.max_mutants,
            max_survivors=options.max_survivors,
            seed=options.seed,
        )
        report.mutation_before = outcome.before
        report.mutation_after = outcome.after
        report.mutation_generated = outcome.records
        layout = outcome.layout or layout
        if outcome.excluded:
            report.stage_skips["mutation_excluded"] = (
                f"{len(outcome.excluded)} already-failing test(s) were left out "
                "of scoring: " + ", ".join(outcome.excluded[:5])
                + (" ..." if len(outcome.excluded) > 5 else "")
            )
        if outcome.budget_exhausted:
            report.stage_skips["mutation_budget"] = (
                "scoring stopped at the phase time budget "
                f"({config.mutation_budget_s}s); the remaining mutants are "
                "recorded as unscored, not as survivors"
            )
        if outcome.skipped_reason:
            # A stage that declined to run has to say so somewhere the reader
            # will look. This used to exist only as a console line, so the
            # report simply showed nothing for stage 8 and left the reader to
            # guess whether it had run and found nothing.
            report.stage_skips["mutation"] = outcome.skipped_reason
            logger.info("mutation phase skipped: %s", outcome.skipped_reason)

    report.layout = layout
    if llm is not None:
        report.prompt_tokens = llm.usage.prompt_tokens
        report.completion_tokens = llm.usage.completion_tokens
        report.llm_calls = llm.usage.calls
        report.cost_usd = llm.usage.cost_usd
    report.duration_s = time.time() - started
    return report


def _repair_again(
    layout,
    environment,
    config: AutefConfig,
    llm: Optional[LLMClient],
    cache: Optional[SignatureCache],
    report: RunReport,
    max_tests: Optional[int],
) -> None:
    """Run the repair loop over whatever is failing now, and fold it in.

    ``before`` is left alone: it describes the project as it arrived, and a
    second pass must not rewrite that. Only the records and the final suite
    state are updated.
    """
    orchestrator = RepairOrchestrator(layout, environment, config, llm, cache=cache)
    second = orchestrator.run(max_tests=max_tests)
    report.records.extend(second.records)
    if second.after is not None:
        report.after = second.after


def _dry_run_report(
    layout: ProjectLayout,
    environment: Environment,
    config: AutefConfig,
    started: float,
) -> RunReport:
    from .resolver import resolve_all
    from .runner import TestRunner

    runner = TestRunner(layout, environment, config)
    before = runner.run_suite()
    resolve_all(list(before.failures) + list(before.collection_errors), layout)

    report = RunReport(project=layout.name, layout=layout, before=before, arm="dry-run")
    report.duration_s = time.time() - started
    if not before.ran:
        report.error = (
            "The suite could not be executed. " + before.stdout_tail[-500:]
        )
    return report


def summarise(report: RunReport) -> str:
    """A short human-readable summary of a run."""
    lines = [f"Project: {report.project}  (arm: {report.arm})"]

    if report.layout:
        lines.append(
            f"  layout      : {report.layout.layout_style}, "
            f"{len(report.layout.test_files)} test files, "
            f"installable={report.layout.installable}"
        )
    if report.error:
        lines.append(f"  ERROR       : {report.error}")

    if report.records and not report.model_calls_succeeded:
        # Otherwise this reads as "the agents tried and could not fix anything".
        reason = report.model_failure_reason() or "no successful model call"
        lines.append(
            "  MODEL       : never reached, so nothing here was diagnosed or "
            "repaired by an agent."
        )
        lines.append(f"                {reason[:220]}")

    if report.before:
        lines.append(
            f"  before      : {len(report.before.passed)} passed, "
            f"{len(report.before.failures)} failed, "
            f"{len(report.before.collection_errors)} collection errors"
        )
    if report.after:
        lines.append(
            f"  after       : {len(report.after.passed)} passed, "
            f"{len(report.after.failures)} failed"
        )

    if report.generated:
        accepted = [g for g in report.generated if g.accepted]
        lines.append(
            f"  generated   : {len(accepted)}/{len(report.generated)} file(s) kept, "
            f"{sum(g.tests_collected for g in accepted)} test(s)"
        )

    if report.coverage_before is not None:
        before, after = report.coverage_before, report.coverage_after or report.coverage_before
        if before.measured:
            lines.append(
                f"  coverage    : {before.line_rate:.0%} -> {after.line_rate:.0%} lines, "
                f"{before.branch_rate:.0%} -> {after.branch_rate:.0%} branches"
            )
        else:
            lines.append(f"  coverage    : not measured ({before.error})")

    if report.mutation_before is not None:
        before = report.mutation_before
        after = report.mutation_after or before
        if before.measured:
            kept = [g for g in report.mutation_generated if g.accepted]
            lines.append(
                f"  mutation    : {before.score:.0%} -> {after.score:.0%} "
                f"({before.killed}/{before.total} -> {after.killed}/{after.total} killed), "
                f"{len(kept)} verified killer test(s)"
            )
        else:
            lines.append(f"  mutation    : not measured ({before.error})")

    if report.records:
        fixed = [r for r in report.records if r.fixed]
        weakened = [r for r in report.records if r.weakened]
        regressed = [r for r in report.records if r.caused_regression]
        skipped = [r for r in report.records if r.skipped_reason]
        attempts = [r.attempts_used for r in fixed]
        lines += [
            f"  repaired    : {len(fixed)}/{len(report.records)}",
            f"  skipped     : {len(skipped)} (not repairable by editing the test)",
            f"  mean attempts to fix: "
            f"{sum(attempts) / len(attempts):.2f}" if attempts else
            "  mean attempts to fix: n/a",
            f"  weakened    : {len(weakened)}",
            f"  regressions : {len(regressed)}",
            f"  cost        : ${report.cost_usd:.4f} "
            f"({report.prompt_tokens} in / {report.completion_tokens} out)",
        ]
    lines.append(f"  duration    : {report.duration_s:.1f}s")
    return "\n".join(lines)
