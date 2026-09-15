"""Data model shared by every stage of the pipeline.

Everything that crosses a stage boundary is a dataclass here, so a run can be
serialised to JSON and replayed or scored without rerunning the LLM.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class RootCause(str, enum.Enum):
    """Why a test failed.

    The repair strategy ladder is keyed off these, so the taxonomy is
    deliberately coarse: each member has to map to a materially different
    repair, otherwise it is not worth distinguishing.
    """

    IMPORT_ERROR = "import_error"
    COLLECTION_ERROR = "collection_error"
    ASSERTION_MISMATCH = "assertion_mismatch"
    MOCK_MISCONFIGURATION = "mock_misconfiguration"
    FIXTURE_SETUP_ERROR = "fixture_setup_error"
    API_MISUSE = "api_misuse"
    ENVIRONMENT_DEPENDENCY = "environment_dependency"
    PRODUCTION_BUG = "production_bug"
    FLAKY_NONDETERMINISM = "flaky_nondeterminism"
    UNKNOWN = "unknown"


#: Causes we refuse to "repair" by editing the test. A production bug means the
#: test is right and the source is wrong; an environment dependency is outside
#: the stated scope. Editing the test in either case manufactures a false pass.
NON_REPAIRABLE = frozenset(
    {RootCause.PRODUCTION_BUG, RootCause.ENVIRONMENT_DEPENDENCY}
)


class Outcome(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class Frame:
    """One traceback frame, as reported by pytest."""

    path: str
    lineno: int
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class TestFailure:
    """A single failing test. One observation in the evaluation."""

    #: Stops pytest trying to collect this dataclass as a test class.
    __test__ = False

    nodeid: str
    outcome: Outcome
    exception_type: str = ""
    exception_message: str = ""
    longrepr: str = ""
    frames: List[Frame] = field(default_factory=list)
    phase: str = "call"
    duration: float = 0.0

    # Filled in by the resolver.
    test_file: Optional[str] = None
    test_function: Optional[str] = None
    test_class: Optional[str] = None
    source_files: List[str] = field(default_factory=list)

    @property
    def test_id(self) -> str:
        return self.nodeid

    def signature(self) -> str:
        """Stable hash of the *shape* of this failure.

        Literals, memory addresses, paths, line numbers and hex ids are
        scrubbed so that the same defect appearing in twenty tests collapses to
        one signature. Used as the cache key for strategy reuse.
        """
        text = f"{self.exception_type}|{_normalise(self.exception_message)}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["outcome"] = self.outcome.value
        return d


_SCRUB_PATTERNS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"[A-Za-z]:[\\/][^\s'\":]+"), "<path>"),
    (re.compile(r"(?<![\w.])/[^\s'\":]+"), "<path>"),
    (re.compile(r"\bline \d+\b"), "line <n>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"'[^']*'"), "<str>"),
    (re.compile(r"\s+"), " "),
]


def _normalise(message: str) -> str:
    """Strip run-specific detail so equivalent failures hash alike."""
    text = message or ""
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text.strip().lower()[:300]


@dataclass
class Diagnosis:
    """Output of the Failure Analysis Agent."""

    root_cause: RootCause
    confidence: float
    at_fault: str  # "test" | "source" | "environment" | "unknown"
    explanation: str = ""
    evidence: List[str] = field(default_factory=list)
    heuristic_cause: Optional[RootCause] = None
    llm_agreed: bool = True
    #: False when the model was never reached and this is the static classifier's
    #: label alone. Without it, a fallback diagnosis is indistinguishable from
    #: one the model produced -- and ``llm_agreed=False`` then reads as "the
    #: model disagreed" when the model never answered.
    model_answered: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["root_cause"] = self.root_cause.value
        d["heuristic_cause"] = (
            self.heuristic_cause.value if self.heuristic_cause else None
        )
        return d


@dataclass
class Strategy:
    """One rung of a repair ladder, chosen by the Repair Strategy Agent."""

    id: str
    label: str
    rung: int
    system_prompt: str
    #: What to put in front of the model. Controls cost as much as quality.
    include_source: bool = True
    include_full_test_file: bool = False
    include_project_tree: bool = False
    include_sibling_tests: bool = False
    #: Whether the model rewrites just the function or the whole test file.
    scope: str = "function"  # "function" | "file"

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("system_prompt", None)  # noisy in reports
        return d


@dataclass
class WeakeningReport:
    """Did the repair pass by actually fixing the test, or by gutting it?"""

    weakened: bool = False
    reasons: List[str] = field(default_factory=list)
    asserts_before: int = 0
    asserts_after: int = 0
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class RepairAttempt:
    """One rung attempted: the patch, the re-run, and the verdict."""

    attempt: int
    strategy_id: str
    strategy_label: str
    applied: bool = False
    verified_pass: bool = False
    rejected_reason: Optional[str] = None
    new_failure: Optional[str] = None
    regressions: List[str] = field(default_factory=list)
    weakening: Optional[WeakeningReport] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    patch_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["weakening"] = self.weakening.to_dict() if self.weakening else None
        return d


@dataclass
class RepairRecord:
    """The full repair history for one failing test."""

    nodeid: str
    signature: str
    diagnosis: Optional[Diagnosis] = None
    attempts: List[RepairAttempt] = field(default_factory=list)
    fixed: bool = False
    skipped_reason: Optional[str] = None
    #: Where the test lives, copied off the resolved failure. Carried here so a
    #: later pass can act on the test file without re-resolving the node id.
    test_file: Optional[str] = None
    test_function: Optional[str] = None
    test_class: Optional[str] = None
    cache_hit: bool = False
    final_strategy_id: Optional[str] = None

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    @property
    def cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    @property
    def weakened(self) -> bool:
        """True when the accepted fix weakened the test."""
        if not self.fixed or not self.attempts:
            return False
        last = self.attempts[-1]
        return bool(last.weakening and last.weakening.weakened)

    @property
    def caused_regression(self) -> bool:
        return bool(self.fixed and self.attempts and self.attempts[-1].regressions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "signature": self.signature,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "fixed": self.fixed,
            "skipped_reason": self.skipped_reason,
            "cache_hit": self.cache_hit,
            "final_strategy_id": self.final_strategy_id,
            "attempts_used": self.attempts_used,
            "cost_usd": round(self.cost_usd, 6),
            "weakened": self.weakened,
            "caused_regression": self.caused_regression,
        }


@dataclass
class SuiteResult:
    """Outcome of running a whole test suite once."""

    passed: List[str] = field(default_factory=list)
    failures: List[TestFailure] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    collection_errors: List[TestFailure] = field(default_factory=list)
    duration_s: float = 0.0
    returncode: int = 0
    stdout_tail: str = ""
    ran: bool = True
    #: The run was killed at the timeout rather than finishing. Distinct from
    #: ``ran=False`` in general: "pytest never started" and "pytest ran for
    #: fifteen minutes and was still going" need different words, and the
    #: ``[timed out]`` marker sits at the head of a tail that is sliced from
    #: the end, so it is invisible exactly when it matters.
    timed_out: bool = False

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failures) + len(self.skipped)

    @property
    def reported(self) -> int:
        """Everything pytest told us about, collection errors included.

        ``total`` counts tests. A suite whose every test file fails to import
        has no tests but is not a silent run -- it reported a collection error,
        which is a repairable failure and must not be mistaken for "pytest
        never started".
        """
        return self.total + len(self.collection_errors)

    @property
    def failing_ids(self) -> List[str]:
        return [f.nodeid for f in self.failures]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": [f.to_dict() for f in self.failures],
            "skipped": self.skipped,
            "collection_errors": [f.to_dict() for f in self.collection_errors],
            "duration_s": round(self.duration_s, 3),
            "returncode": self.returncode,
            "total": self.total,
            "ran": self.ran,
        }


@dataclass
class ProjectLayout:
    """What we worked out about an uploaded project."""

    name: str
    root: str
    import_roots: List[str] = field(default_factory=list)
    test_roots: List[str] = field(default_factory=list)
    source_roots: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    dependency_files: List[str] = field(default_factory=list)
    declared_dependencies: List[str] = field(default_factory=list)
    installable: bool = False  # has pyproject.toml / setup.py
    layout_style: str = "flat"  # "src" | "flat" | "package"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class GeneratedTest:
    """One test file written for a module that had none.

    v1 generated tests too; what it could not do was say whether the result was
    usable. A generated file that does not parse, or that pytest cannot collect,
    is recorded here as rejected rather than counted as coverage of the module.
    """

    module: str
    module_import: str
    test_file: str = ""
    units: List[str] = field(default_factory=list)
    accepted: bool = False
    tests_collected: int = 0
    tests_passing: int = 0
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class FileCoverage:
    """Coverage of one source file, and precisely what is not covered."""

    path: str
    statements: int = 0
    covered_statements: int = 0
    missing_lines: List[int] = field(default_factory=list)
    branches: int = 0
    covered_branches: int = 0
    missing_branches: List[List[int]] = field(default_factory=list)

    @property
    def line_rate(self) -> float:
        return self.covered_statements / self.statements if self.statements else 1.0

    @property
    def branch_rate(self) -> float:
        return self.covered_branches / self.branches if self.branches else 1.0

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data["line_rate"] = round(self.line_rate, 4)
        data["branch_rate"] = round(self.branch_rate, 4)
        return data


@dataclass
class CoverageSnapshot:
    """Line and branch coverage for the project at one point in time."""

    measured: bool = False
    statements: int = 0
    covered_statements: int = 0
    branches: int = 0
    covered_branches: int = 0
    files: List[FileCoverage] = field(default_factory=list)
    error: Optional[str] = None
    duration_s: float = 0.0

    @property
    def line_rate(self) -> float:
        return self.covered_statements / self.statements if self.statements else 0.0

    @property
    def branch_rate(self) -> float:
        return self.covered_branches / self.branches if self.branches else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "measured": self.measured,
            "statements": self.statements,
            "covered_statements": self.covered_statements,
            "branches": self.branches,
            "covered_branches": self.covered_branches,
            "line_rate": round(self.line_rate, 4),
            "branch_rate": round(self.branch_rate, 4),
            "files": [f.to_dict() for f in self.files],
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class Mutant:
    """One deliberate change to the source, and whether the suite noticed."""

    file: str
    lineno: int
    operator: str
    original: str
    mutated: str
    killed: bool = False
    killed_by: Optional[str] = None
    #: Set when a test was written specifically to kill this mutant.
    killed_after_generation: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class MutationSnapshot:
    """Mutation score for the project, and which mutants survived."""

    measured: bool = False
    mutants: List[Mutant] = field(default_factory=list)
    error: Optional[str] = None
    duration_s: float = 0.0
    #: True when scoring stopped early against its time budget, so ``unscored``
    #: is a reflection of the clock rather than of the mutants.
    budget_exhausted: bool = False

    @property
    def total(self) -> int:
        return len(self.mutants)

    @property
    def killed(self) -> int:
        return sum(1 for m in self.mutants if m.killed)

    @property
    def unscored(self) -> int:
        """Mutants no verdict was reached on."""
        return sum(1 for m in self.mutants if m.error)

    @property
    def scored(self) -> int:
        return self.total - self.unscored

    @property
    def survived(self) -> int:
        """Mutants the suite ran against and did not catch.

        A mutant that could not be applied, or that ran out of budget, is not a
        survivor: nothing was learned about the suite from it. Counting those
        as survivors understates the score and invents evidence of weakness
        that was never measured.
        """
        return sum(1 for m in self.mutants if not m.killed and not m.error)

    @property
    def score(self) -> float:
        """Killed over *scored*, not over total."""
        return self.killed / self.scored if self.scored else 0.0

    def survivors(self) -> List[Mutant]:
        return [m for m in self.mutants if not m.killed and not m.error]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "measured": self.measured,
            "total": self.total,
            "scored": self.scored,
            "unscored": self.unscored,
            "killed": self.killed,
            "survived": self.survived,
            "score": round(self.score, 4),
            "budget_exhausted": self.budget_exhausted,
            "mutants": [m.to_dict() for m in self.mutants],
            "error": self.error,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class RunReport:
    """Everything one end-to-end run produced."""

    project: str
    layout: Optional[ProjectLayout] = None
    arm: str = "autef2"
    before: Optional[SuiteResult] = None
    after: Optional[SuiteResult] = None
    records: List[RepairRecord] = field(default_factory=list)

    #: Tests written for modules that had none.
    generated: List[GeneratedTest] = field(default_factory=list)
    #: Tests written to cover lines and branches the suite missed.
    coverage_generated: List[GeneratedTest] = field(default_factory=list)
    #: Tests written to kill mutants the suite did not notice.
    mutation_generated: List[GeneratedTest] = field(default_factory=list)

    coverage_before: Optional[CoverageSnapshot] = None
    coverage_after: Optional[CoverageSnapshot] = None
    mutation_before: Optional[MutationSnapshot] = None
    mutation_after: Optional[MutationSnapshot] = None

    #: Tests this framework wrote, could not repair, and took back out. Kept
    #: on the report because a removal the reader cannot see is indistinguishable
    #: from a test that never existed.
    quarantined: List[Dict[str, Any]] = field(default_factory=list)
    #: Why a stage did not run, keyed by stage name. A stage that declines to
    #: run is not the same as a stage that ran and found nothing, and the
    #: reader has to be able to tell.
    stage_skips: Dict[str, str] = field(default_factory=dict)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Model round trips. Reported because two arms can cost the same while one
    #: needed three times as many calls to get there.
    llm_calls: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: Optional[str] = None

    @property
    def model_calls_succeeded(self) -> bool:
        """Did any request to the model actually return?

        A run where every call failed produces a report full of zeros that reads
        exactly like a run where the model was asked and had nothing to offer.
        The two need telling apart before any number here is quoted.
        """
        if self.llm_calls or self.prompt_tokens:
            return True
        return any(
            attempt.prompt_tokens or attempt.completion_tokens
            for record in self.records
            for attempt in record.attempts
        )

    def model_failure_reason(self) -> Optional[str]:
        """Why the model could not be reached, if that is what happened."""
        if self.model_calls_succeeded:
            return None

        for record in self.records:
            for attempt in record.attempts:
                reason = attempt.rejected_reason or ""
                if "model call failed" in reason:
                    return reason
        for group in (self.generated, self.coverage_generated, self.mutation_generated):
            for written in group:
                if written.error and "model call failed" in written.error:
                    return written.error
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "arm": self.arm,
            "layout": self.layout.to_dict() if self.layout else None,
            "before": self.before.to_dict() if self.before else None,
            "after": self.after.to_dict() if self.after else None,
            "records": [r.to_dict() for r in self.records],
            "generated": [g.to_dict() for g in self.generated],
            "coverage_generated": [g.to_dict() for g in self.coverage_generated],
            "mutation_generated": [g.to_dict() for g in self.mutation_generated],
            "coverage_before": self.coverage_before.to_dict() if self.coverage_before else None,
            "coverage_after": self.coverage_after.to_dict() if self.coverage_after else None,
            "mutation_before": self.mutation_before.to_dict() if self.mutation_before else None,
            "mutation_after": self.mutation_after.to_dict() if self.mutation_after else None,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_calls": self.llm_calls,
            "cost_usd": round(self.cost_usd, 6),
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
        }
