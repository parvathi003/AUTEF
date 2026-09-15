"""The enhancement phases: generate, cover, mutate.

v1's nine-step flow was generate -> execute -> measure coverage -> improve
coverage -> fix failures -> measure mutation -> improve mutation. All of it is
kept. What changes is that every step now works from the project AUTEF was
given rather than from ``source_files/InsuranceApp_Modified``, runs in that
project's own virtualenv, and checks its own output before claiming it worked.

Each phase is a class with one ``run``. They share three rules:

* **nothing is trusted until it is run.** A generated file that cannot be
  collected, a coverage test that does not raise coverage, a mutation test that
  does not actually kill its mutant -- each is measured and reported as such.
* **the layout is re-derived after writing.** New test files change the test
  roots, so the caller gets an updated layout back rather than a stale one.
* **failures produced here are input, not error.** A generated test that fails
  is exactly what the repair loop exists for; the two compose.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .agents.coverage_improve import CoverageImprovementAgent
from .agents.generation import TestGenerationAgent
from .agents.mutation_kill import MutationKillAgent
from .chunker import (
    ModuleUnits,
    module_import_name,
    modules_without_tests,
    testable_modules,
)
from .config import AutefConfig
from .coverage_tool import CoverageTool, gaps
from .ingest import analyse
from .llm import LLMClient
from .models import (
    CoverageSnapshot,
    GeneratedTest,
    Mutant,
    MutationSnapshot,
    ProjectLayout,
)
from .mutation import MutationError, Mutator
from .runner import TestRunner
from .venv_manager import Environment

logger = logging.getLogger(__name__)


@dataclass
class EnhanceOptions:
    """Which of v1's phases to run, and how much of each.

    All off by default except ``generate_if_empty``: a project with no tests at
    all has nothing for the repair loop to do, and silently doing nothing is the
    least useful possible outcome.
    """

    generate: bool = False
    #: Generate even when not asked, if the project ships no tests.
    generate_if_empty: bool = True
    coverage: bool = False
    mutation: bool = False

    max_modules: Optional[int] = 5
    max_coverage_files: Optional[int] = 3
    max_mutants: int = 20
    max_survivors: int = 5
    seed: int = 1337

    def any_enabled(self) -> bool:
        return self.generate or self.generate_if_empty or self.coverage or self.mutation


@dataclass
class GenerationOutcome:
    """What a generation pass produced."""

    records: List[GeneratedTest] = field(default_factory=list)
    layout: Optional[ProjectLayout] = None
    considered: int = 0
    duration_s: float = 0.0

    @property
    def accepted(self) -> List[GeneratedTest]:
        return [r for r in self.records if r.accepted]

    @property
    def tests_added(self) -> int:
        return sum(r.tests_collected for r in self.accepted)


class GenerationPhase:
    """Write tests for modules that have none, then see what they do."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        llm: Optional[LLMClient],
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.llm = llm
        self.agent = TestGenerationAgent(llm, config, layout)
        self.runner = TestRunner(layout, environment, config)

    def plan(self, *, max_modules: Optional[int] = None) -> List[ModuleUnits]:
        """Modules to generate for, most testable surface first.

        Modules with no matching test file come first. If every module already
        has one, the untested-by-name filter has nothing to say and all testable
        modules are offered -- generating a second file for a module that has
        tests is still useful when coverage is low, and the caller decides.
        """
        planned = modules_without_tests(self.layout, limit=max_modules)
        if not planned:
            planned = testable_modules(self.layout, limit=max_modules)
        return planned

    def run(self, *, max_modules: Optional[int] = None) -> GenerationOutcome:
        started = time.time()
        outcome = GenerationOutcome(layout=self.layout)

        modules = self.plan(max_modules=max_modules)
        outcome.considered = len(modules)
        if not modules:
            logger.info("No modules with testable functions or classes were found")
            outcome.duration_s = time.time() - started
            return outcome

        logger.info("Generating tests for %d module(s)", len(modules))
        for index, module in enumerate(modules, start=1):
            logger.info(
                "[%d/%d] %s (%d unit(s))",
                index, len(modules), module.import_name, len(module.units),
            )
            record = self.agent.generate(module)
            if record.accepted:
                self._validate(record)
            else:
                logger.info("  not generated: %s", record.error)
            outcome.records.append(record)

        if outcome.accepted:
            # New files mean new test roots; the runner must see them.
            outcome.layout = analyse(self.layout.root, name_hint=self.layout.name)
            self.layout = outcome.layout

        outcome.duration_s = time.time() - started
        logger.info(
            "Generated %d file(s), %d test(s) collected",
            len(outcome.accepted), outcome.tests_added,
        )
        return outcome

    # -- validation -------------------------------------------------------

    def _validate(self, record: GeneratedTest) -> None:
        """Run the file just written and record what it actually does.

        A file is kept even when its tests fail: that is the repair loop's input.
        It is only discarded when pytest could not run it at all, since such a
        file is not a test suite by any reading and would sit in the project
        breaking every later run.
        """
        result = self.runner.run_file(record.test_file)

        if not result.ran:
            record.error = (
                "pytest could not run the generated file: "
                + result.stdout_tail[-300:]
            )
            self.agent.revert(record)
            return

        record.tests_collected = (
            len(result.passed) + len(result.failures) + len(result.skipped)
        )
        record.tests_passing = len(result.passed)

        if record.tests_collected == 0 and not result.collection_errors:
            record.error = "pytest collected no tests from the generated file"
            self.agent.revert(record)
            return

        logger.info(
            "  %d collected, %d passing%s",
            record.tests_collected,
            record.tests_passing,
            " (rest go to the repair loop)"
            if record.tests_collected > record.tests_passing
            else "",
        )


def generate_tests(
    layout: ProjectLayout,
    environment: Environment,
    config: AutefConfig,
    llm: Optional[LLMClient],
    *,
    max_modules: Optional[int] = None,
) -> Tuple[GenerationOutcome, ProjectLayout]:
    """Convenience entry point: generate, and hand back the refreshed layout."""
    phase = GenerationPhase(layout, environment, config, llm)
    outcome = phase.run(max_modules=max_modules)
    return outcome, (outcome.layout or layout)


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


@dataclass
class CoverageOutcome:
    """Coverage before and after the tests written to improve it."""

    before: Optional[CoverageSnapshot] = None
    after: Optional[CoverageSnapshot] = None
    records: List[GeneratedTest] = field(default_factory=list)
    layout: Optional[ProjectLayout] = None
    duration_s: float = 0.0

    @property
    def accepted(self) -> List[GeneratedTest]:
        return [r for r in self.records if r.accepted]

    @property
    def line_gain(self) -> float:
        if not (self.before and self.after and self.before.measured and self.after.measured):
            return 0.0
        return self.after.line_rate - self.before.line_rate

    @property
    def branch_gain(self) -> float:
        if not (self.before and self.after and self.before.measured and self.after.measured):
            return 0.0
        return self.after.branch_rate - self.before.branch_rate


class CoveragePhase:
    """Measure coverage, write tests for the gaps, measure again."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        llm: Optional[LLMClient],
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.llm = llm
        self.agent = CoverageImprovementAgent(llm, config, layout)
        self.runner = TestRunner(layout, environment, config)

    def measure(self) -> CoverageSnapshot:
        return CoverageTool(self.layout, self.environment, self.config).measure()

    def run(self, *, max_files: Optional[int] = None) -> CoverageOutcome:
        started = time.time()
        outcome = CoverageOutcome(layout=self.layout)

        outcome.before = self.measure()
        if not outcome.before.measured:
            logger.warning("Coverage not measured: %s", outcome.before.error)
            outcome.duration_s = time.time() - started
            return outcome

        targets = gaps(outcome.before, limit=max_files)
        if not targets:
            logger.info("Nothing uncovered; no coverage tests needed")
            outcome.after = outcome.before
            outcome.duration_s = time.time() - started
            return outcome

        logger.info("Writing coverage tests for %d file(s)", len(targets))
        for index, target in enumerate(targets, start=1):
            logger.info(
                "[%d/%d] %s (%d missing line(s), %d missing branch(es))",
                index, len(targets), Path(target.path).name,
                len(target.missing_lines), len(target.missing_branches),
            )
            record = self.agent.generate(
                target,
                import_name=module_import_name(target.path, self.layout),
                existing_tests=self._existing_tests_for(target.path),
            )
            if record.accepted:
                self._validate(record)
            else:
                logger.info("  not written: %s", record.error)
            outcome.records.append(record)

        if outcome.accepted:
            outcome.layout = analyse(self.layout.root, name_hint=self.layout.name)
            self.layout = outcome.layout
            self.runner = TestRunner(self.layout, self.environment, self.config)
            outcome.after = self.measure()
        else:
            outcome.after = outcome.before

        outcome.duration_s = time.time() - started
        logger.info(
            "coverage %.1f%% -> %.1f%% lines, %.1f%% -> %.1f%% branches",
            (outcome.before.line_rate * 100), (outcome.after.line_rate * 100),
            (outcome.before.branch_rate * 100), (outcome.after.branch_rate * 100),
        )
        return outcome

    def _validate(self, record: GeneratedTest) -> None:
        result = self.runner.run_file(record.test_file)
        if not result.ran:
            record.error = (
                "pytest could not run the coverage tests: "
                + result.stdout_tail[-300:]
            )
            _remove_generated(record)
            return

        record.tests_collected = (
            len(result.passed) + len(result.failures) + len(result.skipped)
        )
        record.tests_passing = len(result.passed)
        if record.tests_collected == 0 and not result.collection_errors:
            record.error = "no tests were collected from the generated file"
            _remove_generated(record)

    def _existing_tests_for(self, source_path: str) -> str:
        """The project's own tests for this module, so the model does not repeat them."""
        stem = Path(source_path).stem
        for candidate in self.layout.test_files:
            name = Path(candidate).name
            if name in (f"test_{stem}.py", f"{stem}_test.py"):
                try:
                    return Path(candidate).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
        return ""


# ---------------------------------------------------------------------------
# mutation
# ---------------------------------------------------------------------------


@dataclass
class MutationOutcome:
    """Mutation score before and after the tests written to raise it."""

    before: Optional[MutationSnapshot] = None
    after: Optional[MutationSnapshot] = None
    records: List[GeneratedTest] = field(default_factory=list)
    layout: Optional[ProjectLayout] = None
    duration_s: float = 0.0
    skipped_reason: Optional[str] = None
    #: Tests left out of scoring because they were already failing. A mutation
    #: score is only meaningful against tests that pass on unmutated source.
    excluded: List[str] = field(default_factory=list)
    #: True when the phase stopped early because it ran out of time budget.
    budget_exhausted: bool = False

    @property
    def accepted(self) -> List[GeneratedTest]:
        return [r for r in self.records if r.accepted]

    @property
    def newly_killed(self) -> int:
        return sum(1 for m in (self.after.mutants if self.after else []) if m.killed_after_generation)

    @property
    def score_gain(self) -> float:
        if not (self.before and self.after):
            return 0.0
        return self.after.score - self.before.score


class MutationPhase:
    """Mutate the source, see what the suite misses, and write tests for it."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        llm: Optional[LLMClient],
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.llm = llm
        self.agent = MutationKillAgent(llm, config, layout)
        self.runner = TestRunner(layout, environment, config)
        self.mutator = Mutator(layout)
        #: Node ids that pass on unmutated source. Set by ``run`` and used as
        #: the target list for every mutant, so an already-failing test cannot
        #: be mistaken for a mutant being caught.
        self._green: Optional[List[str]] = None

    def score(
        self,
        *,
        max_mutants: int = 20,
        seed: int = 1337,
        budget_s: Optional[float] = None,
    ) -> MutationSnapshot:
        """Apply mutants one at a time and record which the suite catches.

        ``budget_s`` caps the whole phase. Without it one pathological mutant
        can absorb the entire run: negating the predicate of a condition
        variable, say, makes the wait never satisfy, and the mutant sits there
        until a timeout. A mutant that runs out of budget is recorded as
        unscored rather than as surviving, because "the tests did not catch it"
        and "we never found out" are different facts.
        """
        started = time.time()
        snapshot = MutationSnapshot()

        sites = self.mutator.sites(limit=max_mutants, seed=seed)
        if not sites:
            snapshot.error = "no mutable operators or literals were found"
            snapshot.duration_s = time.time() - started
            return snapshot

        logger.info("Scoring %d mutant(s)", len(sites))
        for index, site in enumerate(sites, start=1):
            mutant = self.mutator.describe(site)
            try:
                original = self.mutator.apply(site)
            except MutationError as exc:
                mutant.error = str(exc)
                snapshot.mutants.append(mutant)
                continue

            if budget_s is not None and time.time() - started > budget_s:
                mutant.error = (
                    "not scored: the mutation phase ran out of its time budget"
                )
                snapshot.mutants.append(mutant)
                self.mutator.restore(site, original)
                snapshot.budget_exhausted = True
                continue

            try:
                result = self.runner.run_fail_fast(self._green)
                mutant.killed = bool(result.failures or result.collection_errors)
                if mutant.killed:
                    first = (result.failures or result.collection_errors)[0]
                    mutant.killed_by = first.nodeid
                elif not result.ran:
                    # The suite could not run at all under the mutant, which says
                    # nothing about the tests; do not score it as a kill.
                    mutant.error = "the suite did not run under this mutant"
            finally:
                self.mutator.restore(site, original)

            logger.info(
                "[%d/%d] %s:%d %s -> %s",
                index, len(sites), Path(site.file).name, site.lineno,
                site.operator, "killed" if mutant.killed else "SURVIVED",
            )
            snapshot.mutants.append(mutant)

        snapshot.measured = True
        snapshot.duration_s = time.time() - started
        logger.info(
            "mutation score %d/%d = %.0f%%",
            snapshot.killed, snapshot.total, snapshot.score * 100,
        )
        return snapshot

    def run(
        self,
        *,
        max_mutants: int = 20,
        max_survivors: int = 5,
        seed: int = 1337,
        require_green: Optional[bool] = None,
    ) -> MutationOutcome:
        started = time.time()
        outcome = MutationOutcome(layout=self.layout)
        if require_green is None:
            require_green = self.config.mutation_requires_green

        if require_green:
            baseline = self.runner.run_suite()
            if not baseline.ran:
                outcome.skipped_reason = (
                    "the suite does not run, so mutation testing would measure "
                    "nothing"
                )
                logger.warning("Mutation skipped: %s", outcome.skipped_reason)
                outcome.duration_s = time.time() - started
                return outcome

            # A red test cannot distinguish "the tests caught the mutant" from
            # "that test was already broken", so it must not be scored against.
            # Refusing the whole stage over it -- which is what this used to do
            # -- throws away the hundreds of tests that ARE green, and hands the
            # decision to whichever test happens to be failing. Score the green
            # subset instead, and say plainly which tests were left out.
            if baseline.collection_errors:
                outcome.skipped_reason = (
                    f"{len(baseline.collection_errors)} test file(s) cannot be "
                    "collected. A file that does not import cannot be excluded "
                    "test by test, so no honest subset remains to score."
                )
                logger.warning("Mutation skipped: %s", outcome.skipped_reason)
                outcome.duration_s = time.time() - started
                return outcome

            if baseline.failures:
                outcome.excluded = [f.nodeid for f in baseline.failures]
                logger.warning(
                    "Scoring against the %d green test(s); excluding %d that "
                    "already fail",
                    len(baseline.passed), len(outcome.excluded),
                )
            if not baseline.passed:
                outcome.skipped_reason = (
                    "no test passes, so there is nothing that could catch a "
                    "mutant"
                )
                logger.warning("Mutation skipped: %s", outcome.skipped_reason)
                outcome.duration_s = time.time() - started
                return outcome
            self._green = list(baseline.passed)

        outcome.before = self.score(
            max_mutants=max_mutants,
            seed=seed,
            budget_s=self.config.mutation_budget_s,
        )
        outcome.budget_exhausted = outcome.before.budget_exhausted
        if not outcome.before.measured:
            outcome.skipped_reason = outcome.before.error
            outcome.duration_s = time.time() - started
            return outcome

        survivors = [m for m in outcome.before.survivors() if not m.error][:max_survivors]
        outcome.after = _copy_snapshot(outcome.before)

        if survivors:
            logger.info("Writing tests for %d surviving mutant(s)", len(survivors))
        for index, mutant in enumerate(survivors, start=1):
            logger.info(
                "[%d/%d] %s:%d %s",
                index, len(survivors), Path(mutant.file).name,
                mutant.lineno, mutant.operator,
            )
            record = self.agent.generate(
                mutant,
                import_name=module_import_name(mutant.file, self.layout),
                existing_tests=self._existing_tests_for(mutant.file),
            )
            outcome.records.append(record)
            if not record.accepted:
                logger.info("  not written: %s", record.error)
                continue
            if self._kills(record, mutant, seed=seed, max_mutants=max_mutants):
                _mark_killed(outcome.after, mutant)

        if outcome.accepted:
            outcome.layout = analyse(self.layout.root, name_hint=self.layout.name)
            self.layout = outcome.layout

        outcome.duration_s = time.time() - started
        logger.info(
            "mutation score %.0f%% -> %.0f%% (%d newly killed)",
            outcome.before.score * 100, outcome.after.score * 100,
            outcome.newly_killed,
        )
        return outcome

    # -- verification -----------------------------------------------------

    def _kills(
        self, record: GeneratedTest, mutant: Mutant, *, seed: int, max_mutants: int
    ) -> bool:
        """Does the new test pass on the original and fail on the mutant?

        Both halves matter. A test that fails on both is broken. A test that
        passes on both did not catch the mutant, however plausible it looks --
        and keeping it would raise the reported score without raising the suite's
        actual sensitivity, which is the exact dishonesty this phase exists to
        avoid.
        """
        clean = self.runner.run_file(record.test_file)
        record.tests_collected = (
            len(clean.passed) + len(clean.failures) + len(clean.skipped)
        )
        record.tests_passing = len(clean.passed)

        if not clean.ran or clean.failures or clean.collection_errors:
            record.error = "the new test does not pass on the unmutated source"
            logger.info("  rejected: %s", record.error)
            _remove_generated(record)
            return False

        site = self._site_for(mutant, seed=seed, max_mutants=max_mutants)
        if site is None:
            record.error = "the mutant could not be reapplied to verify the kill"
            return False

        try:
            original = self.mutator.apply(site)
        except MutationError as exc:
            record.error = f"the mutant could not be reapplied: {exc}"
            return False

        try:
            mutated = self.runner.run_file(record.test_file)
            killed = bool(mutated.failures or mutated.collection_errors)
        finally:
            self.mutator.restore(site, original)

        if not killed:
            record.error = (
                "the new test passes with the mutant applied, so it does not "
                "detect it"
            )
            logger.info("  rejected: %s", record.error)
            _remove_generated(record)
            return False

        # Deliberately does not touch ``mutant``: that object belongs to the
        # "before" snapshot, and marking it killed here would rewrite the
        # baseline to match the improvement, hiding the very gain being measured.
        # Only the "after" snapshot is updated, by the caller.
        logger.info("  verified: the new test kills this mutant")
        return True

    def _site_for(self, mutant: Mutant, *, seed: int, max_mutants: int):
        for site in self.mutator.sites(limit=max_mutants, seed=seed):
            if (
                site.file == mutant.file
                and site.lineno == mutant.lineno
                and site.operator == mutant.operator
            ):
                return site
        return None

    def _existing_tests_for(self, source_path: str) -> str:
        stem = Path(source_path).stem
        for candidate in self.layout.test_files:
            if Path(candidate).name in (f"test_{stem}.py", f"{stem}_test.py"):
                try:
                    return Path(candidate).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
        return ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _remove_generated(record: GeneratedTest) -> None:
    """Delete a generated file that did not earn its place."""
    from .agents.generation import GENERATED_MARKER

    if not record.test_file:
        return
    path = Path(record.test_file)
    try:
        if path.is_file() and GENERATED_MARKER in path.read_text(
            encoding="utf-8", errors="replace"
        ):
            path.unlink()
    except OSError as exc:  # pragma: no cover - never fatal
        logger.warning("Could not remove %s: %s", path, exc)
    record.accepted = False


def _copy_snapshot(snapshot: MutationSnapshot) -> MutationSnapshot:
    """An independent copy, so "after" can diverge from "before"."""
    import dataclasses

    return MutationSnapshot(
        measured=snapshot.measured,
        mutants=[dataclasses.replace(m) for m in snapshot.mutants],
        error=snapshot.error,
        duration_s=snapshot.duration_s,
    )


def _mark_killed(snapshot: Optional[MutationSnapshot], mutant: Mutant) -> None:
    if snapshot is None:
        return
    for candidate in snapshot.mutants:
        if (
            candidate.file == mutant.file
            and candidate.lineno == mutant.lineno
            and candidate.operator == mutant.operator
        ):
            candidate.killed = True
            candidate.killed_after_generation = True
            return
