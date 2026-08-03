"""One repository, both versions, side by side.

``benchmark`` answers "across a sample of projects, is v2 better". This answers
the narrower question that is easier to read and to demonstrate: *on this
repository, what did v1 do and what did v2 do, test by test.*

It is the same experiment, not a second one -- ``compare_project`` calls
``run_benchmark`` with a single project, so both arms still get an identical
pristine copy, an identical environment, an identical execution stack, and the
same failing tests. Only the presentation is different.

Three things are reported:

* **the paired table** -- one row per failing test, v1's outcome beside v2's.
  This is where a reader sees *what* changed rather than a percentage.
* **the four paired counts** -- both fixed, v2 only, v1 only, neither. These are
  McNemar's table, and the discordant pair (v2 only, v1 only) is the entire
  evidence about which repair is better. An exact two-sided p-value comes with
  them, computed from the binomial rather than approximated, because these
  counts are small.
* **efficiency** -- cost, model calls, tokens and wall clock, both in total and
  per fix. A cheaper arm that fixes nothing is not efficient, so per-fix is the
  figure that means something.

The honest caveat this module exists to surface: the baseline cannot attempt a
failure that has no test function to replace, so ``model_attempted`` is reported
next to ``observations`` and the fix rate over attempted observations is shown
alongside the headline. A gap that lives entirely in tests v1 never reached is a
statement about seeded fault shapes, not about prompts.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Dict, List, Optional, Sequence

from ..config import AutefConfig
from ..models import RepairRecord, RunReport
from .benchmark import BenchmarkResult, ProjectSpec, run_benchmark
from .metrics import ArmMetrics

logger = logging.getLogger(__name__)

BASELINE = "baseline"
CANDIDATE = "autef2"

#: How the arms are labelled for a reader who thinks in versions, not arms.
ARM_LABELS = {BASELINE: "v1 (single prompt)", CANDIDATE: "v2 (diagnosed)"}


@dataclass
class TestComparison:
    """What each version did to one failing test."""

    nodeid: str
    exception: str = ""
    root_cause: str = ""
    baseline_outcome: str = "not attempted"
    candidate_outcome: str = "not attempted"
    baseline_attempts: int = 0
    candidate_attempts: int = 0
    baseline_fixed: bool = False
    candidate_fixed: bool = False
    baseline_reason: str = ""
    candidate_strategies: List[str] = field(default_factory=list)
    candidate_weakened: bool = False
    baseline_weakened: bool = False

    @property
    def verdict(self) -> str:
        if self.candidate_fixed and not self.baseline_fixed:
            return "v2 only"
        if self.baseline_fixed and not self.candidate_fixed:
            return "v1 only"
        if self.baseline_fixed and self.candidate_fixed:
            return "both"
        return "neither"

    def to_dict(self) -> Dict[str, object]:
        return {
            "nodeid": self.nodeid,
            "exception": self.exception,
            "root_cause": self.root_cause,
            "verdict": self.verdict,
            "v1_outcome": self.baseline_outcome,
            "v2_outcome": self.candidate_outcome,
            "v1_attempts": self.baseline_attempts,
            "v2_attempts": self.candidate_attempts,
            "v1_weakened": self.baseline_weakened,
            "v2_weakened": self.candidate_weakened,
            "v2_strategies": " -> ".join(self.candidate_strategies),
            "v1_reason": self.baseline_reason,
        }


@dataclass
class PairedCounts:
    """McNemar's table for fixed/not-fixed across the two arms."""

    both: int = 0
    candidate_only: int = 0
    baseline_only: int = 0
    neither: int = 0

    @property
    def discordant(self) -> int:
        return self.candidate_only + self.baseline_only

    @property
    def p_value(self) -> Optional[float]:
        """Exact two-sided McNemar p-value, or None with no discordant pairs.

        Exact rather than the chi-square approximation: one repository yields a
        handful of discordant pairs, which is exactly where the approximation
        misleads.
        """
        n = self.discordant
        if n == 0:
            return None
        k = min(self.candidate_only, self.baseline_only)
        tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
        return min(1.0, 2 * tail)

    def to_dict(self) -> Dict[str, object]:
        return {
            "both_fixed": self.both,
            "v2_only": self.candidate_only,
            "v1_only": self.baseline_only,
            "neither": self.neither,
            "discordant": self.discordant,
            "p_value": self.p_value,
        }


@dataclass
class ComparisonResult:
    project: str
    tests: List[TestComparison] = field(default_factory=list)
    counts: PairedCounts = field(default_factory=PairedCounts)
    metrics_by_arm: Dict[str, ArmMetrics] = field(default_factory=dict)
    reports_by_arm: Dict[str, RunReport] = field(default_factory=dict)
    seeded_faults: List[Dict[str, object]] = field(default_factory=list)
    #: Set when fewer faults were seeded than requested. Not an error -- the
    #: run is valid -- but the denominator is not the one the protocol named,
    #: so it has to be said out loud rather than left in a log.
    seeding_note: Optional[str] = None
    output_dir: Optional[str] = None
    error: Optional[str] = None
    #: True when no request to the model succeeded, so there is no comparison.
    model_unavailable: bool = False

    @property
    def baseline(self) -> Optional[ArmMetrics]:
        return self.metrics_by_arm.get(BASELINE)

    @property
    def candidate(self) -> Optional[ArmMetrics]:
        return self.metrics_by_arm.get(CANDIDATE)

    def to_dict(self) -> Dict[str, object]:
        return {
            "project": self.project,
            "error": self.error,
            "counts": self.counts.to_dict(),
            "tests": [t.to_dict() for t in self.tests],
            "metrics": {
                arm: metrics.to_dict() for arm, metrics in self.metrics_by_arm.items()
            },
            "seeded_faults": self.seeded_faults,
            "seeding_note": self.seeding_note,
        }


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def compare_project(
    source: str,
    config: Optional[AutefConfig] = None,
    *,
    inject: int = 0,
    max_tests: Optional[int] = None,
    seed: int = 1337,
    output_dir: Optional[Path] = None,
    fault_kinds: Sequence[str] = (),
) -> ComparisonResult:
    """Run both versions over one repository and pair the results.

    ``inject`` seeds that many faults into currently-passing tests first. Real
    repositories mostly pass, so without seeding there is usually nothing for
    either version to repair and nothing to compare.

    ``fault_kinds`` restricts the mix. Worth setting deliberately: the baseline
    cannot attempt a file-scoped failure at all, so a mix heavy in
    ``broken_import`` hands v2 a margin it did not earn on repair quality.
    """
    config = config or AutefConfig.from_env()
    spec = ProjectSpec(
        name=_label(source), source=source, stratum="single", inject=inject,
        max_tests=max_tests, fault_kinds=tuple(fault_kinds),
    )

    result = run_benchmark(
        [spec],
        config,
        arms=(BASELINE, CANDIDATE),
        seed=seed,
        output_dir=output_dir,
        use_cache=False,
    )
    return from_benchmark(result, project=spec.name)


def from_benchmark(
    result: BenchmarkResult, *, project: Optional[str] = None
) -> ComparisonResult:
    """Pair up an already-run benchmark. Kept separate so the UI can reuse it."""
    reports = {
        arm: reports[0]
        for arm, reports in result.reports_by_arm.items()
        if reports
    }
    name = project or next(
        (r.project for r in reports.values() if r.project), "project"
    )
    comparison = ComparisonResult(
        project=name,
        metrics_by_arm=dict(result.metrics_by_arm),
        reports_by_arm=reports,
        output_dir=result.output_dir,
        seeded_faults=[
            fault.to_dict()
            for faults in result.faults_by_project.values()
            for fault in faults
        ],
        seeding_note="; ".join(result.fault_shortfalls.values()) or None,
    )

    if result.skipped:
        comparison.error = "; ".join(
            f"{entry['project']}: {entry['reason']}" for entry in result.skipped
        )
    for report in reports.values():
        if report.error and not comparison.error:
            comparison.error = report.error

    comparison.model_unavailable = bool(reports) and not any(
        report.model_calls_succeeded for report in reports.values()
    )
    if comparison.model_unavailable:
        reason = next(
            (r.model_failure_reason() for r in reports.values() if r.model_failure_reason()),
            None,
        )
        # Without this, a run where every request failed renders as a tidy table
        # of zeros and "same", which reads as a measured result showing no
        # difference. It is not a result at all.
        comparison.error = (
            "No model call succeeded, so neither version attempted a repair. "
            "Every number below is zero for that reason and none of them "
            "compare anything."
            + (f" The model reported: {reason}" if reason else "")
        )

    comparison.tests = _pair_records(
        reports.get(BASELINE), reports.get(CANDIDATE)
    )
    comparison.counts = _count(comparison.tests)
    return comparison


def _pair_records(
    baseline: Optional[RunReport], candidate: Optional[RunReport]
) -> List[TestComparison]:
    """Join the two arms' records on test id.

    Both arms are offered the same failing tests from the same pristine copy, so
    the ids line up. A test present in one arm only is still reported, with the
    other side left as "not attempted" rather than silently dropped.
    """
    by_baseline = {r.nodeid: r for r in (baseline.records if baseline else [])}
    by_candidate = {r.nodeid: r for r in (candidate.records if candidate else [])}

    failures = {
        f.nodeid: f
        for report in (candidate, baseline)
        if report is not None and report.before is not None
        for f in list(report.before.failures) + list(report.before.collection_errors)
    }

    ordered: List[str] = list(by_candidate) + [
        n for n in by_baseline if n not in by_candidate
    ]

    comparisons: List[TestComparison] = []
    for nodeid in ordered:
        failure = failures.get(nodeid)
        entry = TestComparison(
            nodeid=nodeid,
            exception=(failure.exception_type if failure else ""),
        )
        _fill(entry, by_baseline.get(nodeid), side="baseline")
        _fill(entry, by_candidate.get(nodeid), side="candidate")
        comparisons.append(entry)
    return comparisons


def _fill(entry: TestComparison, record: Optional[RepairRecord], *, side: str) -> None:
    if record is None:
        return

    outcome = _outcome(record)
    if side == "baseline":
        entry.baseline_outcome = outcome
        entry.baseline_attempts = record.attempts_used
        entry.baseline_fixed = record.fixed
        entry.baseline_weakened = record.weakened
        entry.baseline_reason = _why(record)
    else:
        entry.candidate_outcome = outcome
        entry.candidate_attempts = record.attempts_used
        entry.candidate_fixed = record.fixed
        entry.candidate_weakened = record.weakened
        entry.candidate_strategies = [a.strategy_id for a in record.attempts]
        if record.diagnosis is not None:
            entry.root_cause = record.diagnosis.root_cause.value


def _outcome(record: RepairRecord) -> str:
    if record.fixed:
        return "fixed (weakened)" if record.weakened else "fixed"
    if record.skipped_reason:
        return "declined"
    if not record.attempts:
        return "not attempted"
    return "not fixed"


def _why(record: RepairRecord) -> str:
    if record.fixed:
        return ""
    if record.skipped_reason:
        return record.skipped_reason
    for attempt in reversed(record.attempts):
        reason = attempt.rejected_reason or attempt.new_failure
        if reason:
            return reason
    return ""


def _count(tests: Sequence[TestComparison]) -> PairedCounts:
    counts = PairedCounts()
    for test in tests:
        verdict = test.verdict
        if verdict == "both":
            counts.both += 1
        elif verdict == "v2 only":
            counts.candidate_only += 1
        elif verdict == "v1 only":
            counts.baseline_only += 1
        else:
            counts.neither += 1
    return counts


def _label(source: str) -> str:
    """A bare project name from a path or URL.

    Must not return anything containing a separator. The name becomes ingest's
    ``name_hint``, which is joined onto the workspace -- and joining an absolute
    path discards the workspace entirely, which once made a comparison run
    against the user's own directory and seed faults into it.
    """
    text = source.strip().rstrip("/\\")
    if text.lower().endswith(".git"):
        text = text[:-4]
    # Split on both separators: a Windows path has neither forward slashes nor,
    # necessarily, a URL shape.
    name = PurePath(text.replace("\\", "/")).name
    return name or "project"


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------


def render_comparison(result: ComparisonResult) -> str:
    """A readable v1-versus-v2 report for one repository."""
    lines = [f"# {result.project}: v1 versus v2", ""]

    if result.error:
        lines += [f"> {result.error}", ""]

    if result.model_unavailable:
        # Deliberately does not print the tables. Zeros in a comparison table
        # are indistinguishable from a measured tie.
        return "\n".join(
            lines
            + [
                "No comparison can be made from this run. Restore the model's "
                "quota and run it again.",
                "",
            ]
        )

    baseline, candidate = result.baseline, result.candidate
    if baseline is None or candidate is None:
        return "\n".join(lines + ["Both arms are needed for a comparison."])

    lines += _headline(baseline, candidate)
    lines += _efficiency(baseline, candidate)
    lines += _paired(result.counts)
    lines += _per_test(result.tests)
    lines += _caveats(result, baseline, candidate)
    return "\n".join(lines)


def _headline(baseline: ArmMetrics, candidate: ArmMetrics) -> List[str]:
    rows = [
        ("Failing tests offered", str(baseline.observations), str(candidate.observations), ""),
        (
            "Put to the model",
            str(baseline.model_attempted),
            str(candidate.model_attempted),
            "",
        ),
        ("Fixed", str(baseline.fixed), str(candidate.fixed),
         _delta(baseline.fixed, candidate.fixed, integer=True)),
        (
            "Fix rate (of attempted)",
            f"{baseline.fix_rate_attempted:.0%}",
            f"{candidate.fix_rate_attempted:.0%}",
            _points(baseline.fix_rate_attempted, candidate.fix_rate_attempted),
        ),
        (
            "Fix rate (of all offered)",
            f"{baseline.fix_rate_all:.0%}",
            f"{candidate.fix_rate_all:.0%}",
            _points(baseline.fix_rate_all, candidate.fix_rate_all),
        ),
        (
            "Fixes that weakened the test",
            str(baseline.weakened),
            str(candidate.weakened),
            _delta(baseline.weakened, candidate.weakened, integer=True, lower_is_better=True),
        ),
        (
            "Regressions introduced",
            str(baseline.regressions_introduced),
            str(candidate.regressions_introduced),
            _delta(
                baseline.regressions_introduced,
                candidate.regressions_introduced,
                integer=True,
                lower_is_better=True,
            ),
        ),
    ]
    return _table("## Outcome", rows)


def _efficiency(baseline: ArmMetrics, candidate: ArmMetrics) -> List[str]:
    rows = [
        ("Model calls", str(baseline.llm_calls), str(candidate.llm_calls), ""),
        (
            "Calls per fix",
            _num(baseline.calls_per_fix, baseline.fixed, "{:.1f}"),
            _num(candidate.calls_per_fix, candidate.fixed, "{:.1f}"),
            _ratio(baseline.calls_per_fix, candidate.calls_per_fix,
                   baseline.fixed and candidate.fixed),
        ),
        (
            "Tokens",
            f"{baseline.prompt_tokens + baseline.completion_tokens:,}",
            f"{candidate.prompt_tokens + candidate.completion_tokens:,}",
            "",
        ),
        (
            "Tokens per fix",
            _num(baseline.tokens_per_fix, baseline.fixed, "{:,.0f}"),
            _num(candidate.tokens_per_fix, candidate.fixed, "{:,.0f}"),
            _ratio(baseline.tokens_per_fix, candidate.tokens_per_fix,
                   baseline.fixed and candidate.fixed),
        ),
        ("Total cost", f"${baseline.cost_usd:.4f}", f"${candidate.cost_usd:.4f}", ""),
        (
            "Cost per fix",
            _num(baseline.cost_per_fix, baseline.fixed, "${:.4f}"),
            _num(candidate.cost_per_fix, candidate.fixed, "${:.4f}"),
            _ratio(baseline.cost_per_fix, candidate.cost_per_fix,
                   baseline.fixed and candidate.fixed),
        ),
        (
            "Wall clock",
            f"{baseline.duration_s:.0f}s",
            f"{candidate.duration_s:.0f}s",
            "",
        ),
        (
            "Mean attempts per fix",
            _num(baseline.mean_attempts, baseline.fixed, "{:.2f}"),
            _num(candidate.mean_attempts, candidate.fixed, "{:.2f}"),
            "",
        ),
    ]
    return _table("## Efficiency", rows) + [
        "Per-fix figures are the ones to read: an arm that spends nothing and "
        "repairs nothing is cheap, not efficient. v2 costs more per attempt by "
        "construction -- it diagnoses first and re-runs to verify -- so the "
        "question is whether the extra spend buys fixes.",
        "",
    ]


def _paired(counts: PairedCounts) -> List[str]:
    lines = [
        "## Paired outcomes",
        "",
        "| | v2 fixed | v2 not fixed |",
        "| --- | --- | --- |",
        f"| **v1 fixed** | {counts.both} | {counts.baseline_only} |",
        f"| **v1 not fixed** | {counts.candidate_only} | {counts.neither} |",
        "",
    ]
    if counts.discordant == 0:
        lines += [
            "No discordant pairs: the two versions agreed on every test, so "
            "this repository carries no evidence either way.",
            "",
        ]
        return lines

    p_value = counts.p_value
    lines += [
        f"{counts.discordant} test(s) separate the versions: "
        f"{counts.candidate_only} that only v2 fixed, {counts.baseline_only} "
        f"that only v1 fixed.",
        "",
        f"Exact two-sided McNemar p = {p_value:.3f}"
        + (
            " -- not significant on its own; one repository rarely is, which is "
            "what the multi-project benchmark is for."
            if p_value is None or p_value > 0.05
            else " -- significant at the 5% level."
        ),
        "",
    ]
    return lines


def _per_test(tests: Sequence[TestComparison]) -> List[str]:
    if not tests:
        return ["## Per test", "", "_No failing tests were offered._", ""]

    lines = [
        "## Per test",
        "",
        "| Test | Diagnosed cause | v1 | v2 | v2 strategies | Winner |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for test in tests:
        lines.append(
            f"| `{_short(test.nodeid)}` | {test.root_cause or test.exception or '-'} "
            f"| {test.baseline_outcome} | {test.candidate_outcome} "
            f"| {' -> '.join(test.candidate_strategies) or '-'} | {test.verdict} |"
        )
    lines.append("")

    explained = [t for t in tests if t.verdict == "v2 only" and t.baseline_reason]
    if explained:
        lines += ["Where v1 fell short:", ""]
        for test in explained[:10]:
            lines.append(f"- `{_short(test.nodeid)}`: {test.baseline_reason}")
        lines.append("")
    return lines


def _caveats(
    result: ComparisonResult, baseline: ArmMetrics, candidate: ArmMetrics
) -> List[str]:
    lines = ["## Reading this", ""]

    if baseline.no_attempt:
        lines.append(
            f"- v1 never attempted {baseline.no_attempt} of "
            f"{baseline.observations} failing test(s). v1 replaces a failing test "
            "*function*; a failure raised at import time has none, so those are "
            "beyond it by construction rather than by prompt quality. The "
            "fix-rate-of-attempted row is the like-for-like comparison."
        )
    if candidate.weakened:
        lines.append(
            f"- {candidate.weakened} of v2's fixes weakened the assertion. Those "
            "count as fixes in the rate above and should not be read as wins."
        )
    if result.seeded_faults:
        kinds: Dict[str, int] = {}
        for fault in result.seeded_faults:
            kind = str(fault.get("kind", "?"))
            kinds[kind] = kinds.get(kind, 0) + 1
        summary = ", ".join(f"{count} x {kind}" for kind, count in sorted(kinds.items()))
        lines.append(
            f"- Faults were seeded, not naturally occurring: {summary}. The mix "
            "decides the result, so report it alongside the numbers."
        )
    if result.seeding_note:
        lines.append(f"- **Fewer faults than requested**: {result.seeding_note}")
    lines += [
        "- Both arms ran on the same pristine copy, the same virtualenv and the "
        "same pytest/resolver/patcher stack. The only difference is the repair "
        "step, which is what makes the gap attributable to it.",
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _table(title: str, rows: Sequence[tuple]) -> List[str]:
    lines = [
        title,
        "",
        f"| Metric | {ARM_LABELS[BASELINE]} | {ARM_LABELS[CANDIDATE]} | Change |",
        "| --- | --- | --- | --- |",
    ]
    for label, left, right, change in rows:
        lines.append(f"| {label} | {left} | {right} | {change} |")
    lines.append("")
    return lines


def _num(value: float, guard, template: str) -> str:
    if not guard or value != value:  # NaN guard
        return "n/a"
    return template.format(value)


def _delta(before: int, after: int, *, integer=False, lower_is_better=False) -> str:
    difference = after - before
    if difference == 0:
        return "same"
    arrow = "better" if (difference > 0) != lower_is_better else "worse"
    sign = "+" if difference > 0 else ""
    return f"{sign}{difference} ({arrow})"


def _points(before: float, after: float) -> str:
    difference = (after - before) * 100
    if abs(difference) < 0.05:
        return "same"
    return f"{'+' if difference > 0 else ''}{difference:.0f} pts"


def _ratio(before: float, after: float, guard) -> str:
    if not guard or before != before or after != after or after == 0 or before == 0:
        return "n/a"
    if after < before:
        return f"{before / after:.1f}x cheaper"
    if after > before:
        return f"{after / before:.1f}x dearer"
    return "same"


def _short(nodeid: str, limit: int = 70) -> str:
    if len(nodeid) <= limit:
        return nodeid
    return "..." + nodeid[-(limit - 3):]
