"""Scoring: turn run reports into the five numbers the comparison is about.

    fix rate            how many failing tests end up passing
    attempts per fix    how much work a fix took
    regression rate     how often a fix broke something that was passing
    cost per fix        USD of model spend per test actually fixed
    weakening rate      how often a "fix" passed by gutting the assertion

The last one exists because the first four can all be gamed by deleting
assertions, and an automated repair loop will find that shortcut if nothing is
watching for it. Reporting fix rate without weakening rate would be reporting
half a result.

Every metric is computed identically for both arms from the same fields, and
regressions are measured from the suite (passing before, not passing after)
rather than from anything an arm reports about itself.
"""

from __future__ import annotations

import csv
import dataclasses
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from ..models import RunReport


@dataclass
class ArmMetrics:
    """Aggregate results for one arm across all projects."""

    arm: str
    projects: int = 0
    projects_executed: int = 0
    observations: int = 0
    fixed: int = 0
    skipped_non_repairable: int = 0

    #: Observations where the arm actually consulted the model. The baseline
    #: cannot attempt a module-level failure at all -- v1 repaired a test
    #: *function*, and a collection-time import error has none -- so it records
    #: an attempt that spends nothing. Counting those against it would compare
    #: prompts on tests one arm was structurally unable to reach.
    model_attempted: int = 0
    no_attempt: int = 0

    attempts_used: List[int] = field(default_factory=list)
    regressions_introduced: int = 0
    projects_with_regressions: int = 0
    weakened: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    cache_hits: int = 0

    by_cause: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_strategy: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # -- derived ----------------------------------------------------------

    @property
    def repairable(self) -> int:
        """Observations the arm was willing to attempt.

        Tests skipped as production bugs or environment dependencies are
        excluded: refusing to edit a test over a real source defect is correct
        behaviour, and counting it as a miss would penalise exactly the
        judgement we want.
        """
        return self.observations - self.skipped_non_repairable

    @property
    def fix_rate(self) -> float:
        return self.fixed / self.repairable if self.repairable else 0.0

    @property
    def fix_rate_all(self) -> float:
        """Fix rate over every observation, skips included. The strict view."""
        return self.fixed / self.observations if self.observations else 0.0

    @property
    def fix_rate_attempted(self) -> float:
        """Fix rate over the observations the arm actually put to the model.

        This is the like-for-like number when comparing two repair strategies:
        it excludes failures an arm could not reach at all, so the comparison is
        about the repair and not about the shape of the seeded fault.
        """
        return self.fixed / self.model_attempted if self.model_attempted else 0.0

    @property
    def tokens_per_fix(self) -> float:
        total = self.prompt_tokens + self.completion_tokens
        return total / self.fixed if self.fixed else float("nan")

    @property
    def calls_per_fix(self) -> float:
        return self.llm_calls / self.fixed if self.fixed else float("nan")

    @property
    def seconds_per_fix(self) -> float:
        return self.duration_s / self.fixed if self.fixed else float("nan")

    @property
    def mean_attempts(self) -> float:
        return statistics.fmean(self.attempts_used) if self.attempts_used else 0.0

    @property
    def median_attempts(self) -> float:
        return statistics.median(self.attempts_used) if self.attempts_used else 0.0

    @property
    def regression_rate(self) -> float:
        return (
            self.regressions_introduced / self.fixed if self.fixed else 0.0
        )

    @property
    def weakening_rate(self) -> float:
        return self.weakened / self.fixed if self.fixed else 0.0

    @property
    def cost_per_fix(self) -> float:
        return self.cost_usd / self.fixed if self.fixed else float("nan")

    def to_dict(self) -> Dict[str, object]:
        data = dataclasses.asdict(self)
        data.update(
            {
                "fix_rate": round(self.fix_rate, 4),
                "fix_rate_all": round(self.fix_rate_all, 4),
                "fix_rate_attempted": round(self.fix_rate_attempted, 4),
                "tokens_per_fix": round(self.tokens_per_fix, 1) if self.fixed else None,
                "calls_per_fix": round(self.calls_per_fix, 2) if self.fixed else None,
                "seconds_per_fix": round(self.seconds_per_fix, 1) if self.fixed else None,
                "mean_attempts": round(self.mean_attempts, 3),
                "median_attempts": self.median_attempts,
                "regression_rate": round(self.regression_rate, 4),
                "weakening_rate": round(self.weakening_rate, 4),
                "cost_per_fix": round(self.cost_per_fix, 6)
                if self.fixed
                else None,
                "repairable": self.repairable,
            }
        )
        data.pop("attempts_used", None)
        return data


def compute_metrics(arm: str, reports: Sequence[RunReport]) -> ArmMetrics:
    metrics = ArmMetrics(arm=arm, projects=len(reports))

    for report in reports:
        metrics.prompt_tokens += report.prompt_tokens
        metrics.completion_tokens += report.completion_tokens
        metrics.llm_calls += report.llm_calls
        metrics.cost_usd += report.cost_usd
        metrics.duration_s += report.duration_s

        if report.before is None or not report.before.ran:
            continue
        metrics.projects_executed += 1

        for record in report.records:
            metrics.observations += 1
            if record.cache_hit:
                metrics.cache_hits += 1

            cause = (
                record.diagnosis.root_cause.value if record.diagnosis else "undiagnosed"
            )
            bucket = metrics.by_cause.setdefault(cause, {"n": 0, "fixed": 0})
            bucket["n"] += 1

            if _consulted_model(record):
                metrics.model_attempted += 1
            else:
                metrics.no_attempt += 1

            if record.skipped_reason:
                metrics.skipped_non_repairable += 1
                continue

            if record.fixed:
                metrics.fixed += 1
                bucket["fixed"] += 1
                metrics.attempts_used.append(max(record.attempts_used, 1))
                if record.weakened:
                    metrics.weakened += 1

                strategy = record.final_strategy_id or "unknown"
                s_bucket = metrics.by_strategy.setdefault(
                    strategy, {"n": 0, "fixed": 0}
                )
                s_bucket["n"] += 1
                s_bucket["fixed"] += 1

        regressions = _suite_regressions(report)
        if regressions:
            metrics.regressions_introduced += len(regressions)
            metrics.projects_with_regressions += 1

    return metrics


def _consulted_model(record) -> bool:
    """Did the arm actually ask the model to repair this test?

    A record with no attempt, or attempts that spent no tokens, means the arm
    never got as far as a repair -- it could not locate the failing function, or
    it declined the test on sight. Those are not evidence about the prompt.
    """
    return any(
        attempt.prompt_tokens or attempt.completion_tokens
        for attempt in record.attempts
    )


def _suite_regressions(report: RunReport) -> List[str]:
    """Tests that passed before the arm ran and do not pass after it."""
    if report.before is None or report.after is None or not report.after.ran:
        return []
    before_passing = set(report.before.passed)
    after_passing = set(report.after.passed)
    return sorted(before_passing - after_passing)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


def write_observations_csv(
    path: Path, reports_by_arm: Dict[str, Sequence[RunReport]]
) -> Path:
    """One row per failing test per arm -- the unit of analysis.

    This is the file to run significance tests on. Paired on (project, nodeid),
    the two arms give a matched sample, so McNemar's test on fixed/not-fixed is
    the natural comparison rather than comparing two independent proportions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "arm", "project", "nodeid", "signature", "root_cause", "at_fault",
        "confidence", "cache_hit", "attempts", "fixed", "weakened",
        "regression", "skipped_reason", "final_strategy", "cost_usd",
        "prompt_tokens", "completion_tokens",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for arm, reports in reports_by_arm.items():
            for report in reports:
                for record in report.records:
                    diagnosis = record.diagnosis
                    writer.writerow(
                        {
                            "arm": arm,
                            "project": report.project,
                            "nodeid": record.nodeid,
                            "signature": record.signature,
                            "root_cause": diagnosis.root_cause.value if diagnosis else "",
                            "at_fault": diagnosis.at_fault if diagnosis else "",
                            "confidence": round(diagnosis.confidence, 3) if diagnosis else "",
                            "cache_hit": int(record.cache_hit),
                            "attempts": record.attempts_used,
                            "fixed": int(record.fixed),
                            "weakened": int(record.weakened),
                            "regression": int(record.caused_regression),
                            "skipped_reason": record.skipped_reason or "",
                            "final_strategy": record.final_strategy_id or "",
                            "cost_usd": round(record.cost_usd, 6),
                            "prompt_tokens": sum(a.prompt_tokens for a in record.attempts),
                            "completion_tokens": sum(
                                a.completion_tokens for a in record.attempts
                            ),
                        }
                    )
    return path


def render_markdown(
    metrics_by_arm: Dict[str, ArmMetrics],
    reports_by_arm: Optional[Dict[str, Sequence[RunReport]]] = None,
) -> str:
    """A readable comparison table plus per-project detail."""
    arms = list(metrics_by_arm)
    lines = ["# AUTEF evaluation", ""]

    if not arms:
        return "\n".join(lines + ["No results."])

    rows = [
        ("Projects processed", lambda m: f"{m.projects_executed}/{m.projects}"),
        ("Failing tests (observations)", lambda m: str(m.observations)),
        ("Put to the model", lambda m: str(m.model_attempted)),
        ("Never attempted", lambda m: str(m.no_attempt)),
        ("Skipped as not test-repairable", lambda m: str(m.skipped_non_repairable)),
        ("Fixed", lambda m: str(m.fixed)),
        ("Fix rate (of attempted)", lambda m: f"{m.fix_rate_attempted:.1%}"),
        ("Fix rate (of repairable)", lambda m: f"{m.fix_rate:.1%}"),
        ("Fix rate (of all)", lambda m: f"{m.fix_rate_all:.1%}"),
        ("Mean attempts per fix", lambda m: f"{m.mean_attempts:.2f}"),
        ("Median attempts per fix", lambda m: f"{m.median_attempts:.1f}"),
        ("Regressions introduced", lambda m: str(m.regressions_introduced)),
        ("Regressions per fix", lambda m: f"{m.regression_rate:.2f}"),
        ("Fixes that weakened the test", lambda m: str(m.weakened)),
        ("Weakening rate", lambda m: f"{m.weakening_rate:.1%}"),
        ("Total cost (USD)", lambda m: f"${m.cost_usd:.4f}"),
        (
            "Cost per fix (USD)",
            lambda m: f"${m.cost_per_fix:.4f}" if m.fixed else "n/a",
        ),
        ("Model calls", lambda m: str(m.llm_calls)),
        ("Calls per fix", lambda m: f"{m.calls_per_fix:.1f}" if m.fixed else "n/a"),
        ("Tokens", lambda m: f"{m.prompt_tokens + m.completion_tokens:,}"),
        (
            "Tokens per fix",
            lambda m: f"{m.tokens_per_fix:,.0f}" if m.fixed else "n/a",
        ),
        ("Cache hits", lambda m: str(m.cache_hits)),
        ("Wall clock (s)", lambda m: f"{m.duration_s:.0f}"),
        (
            "Seconds per fix",
            lambda m: f"{m.seconds_per_fix:.0f}" if m.fixed else "n/a",
        ),
    ]

    lines.append("| Metric | " + " | ".join(arms) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in arms) + " |")
    for label, getter in rows:
        values = " | ".join(getter(metrics_by_arm[a]) for a in arms)
        lines.append(f"| {label} | {values} |")

    lines += ["", "## Fix rate by diagnosed root cause", ""]
    causes = sorted({c for m in metrics_by_arm.values() for c in m.by_cause})
    if causes:
        lines.append("| Root cause | " + " | ".join(arms) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in arms) + " |")
        for cause in causes:
            cells = []
            for arm in arms:
                bucket = metrics_by_arm[arm].by_cause.get(cause)
                cells.append(
                    f"{bucket['fixed']}/{bucket['n']}" if bucket else "-"
                )
            lines.append(f"| {cause} | " + " | ".join(cells) + " |")
    else:
        lines.append("_No diagnosed causes recorded._")

    if reports_by_arm:
        lines += ["", "## Per project", ""]
        lines.append("| Project | Arm | Failing | Fixed | Weakened | Regressions | Cost |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for arm in arms:
            for report in reports_by_arm.get(arm, []):
                fixed = sum(1 for r in report.records if r.fixed)
                weakened = sum(1 for r in report.records if r.weakened)
                regressions = len(_suite_regressions(report))
                lines.append(
                    f"| {report.project} | {arm} | {len(report.records)} | "
                    f"{fixed} | {weakened} | {regressions} | "
                    f"${report.cost_usd:.4f} |"
                )

    lines += [
        "",
        "## Reading these numbers",
        "",
        "- Fix rate alone is not a result. A repair loop can raise it by "
        "deleting assertions; the weakening rate is the check on that.",
        "- Regressions are measured from the suite (passing before, not "
        "passing after), identically for both arms.",
        "- Observations are paired on (project, test id) across arms, so the "
        "appropriate significance test is McNemar's on the fixed/not-fixed "
        "table, not a two-proportion z-test.",
        "- Seeded faults and naturally occurring failures should be reported "
        "separately; seeded faults are by construction the kind of failure "
        "these repairs target.",
    ]
    return "\n".join(lines)


def flatten_for_json(
    metrics_by_arm: Dict[str, ArmMetrics]
) -> Dict[str, Dict[str, object]]:
    return {arm: metrics.to_dict() for arm, metrics in metrics_by_arm.items()}
