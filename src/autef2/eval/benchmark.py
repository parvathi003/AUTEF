"""Run both arms over a sample of projects and score them.

The controls that make the comparison mean something:

* **identical starting state** -- a pristine copy is taken after any faults are
  seeded, and the project is restored from it before each arm, so neither arm
  ever sees the other's edits;
* **identical environment** -- the virtualenv is built once per project and
  shared, so a difference is never an artefact of one arm getting a different
  dependency resolution;
* **identical execution stack** -- both arms run through the same pytest
  runner, resolver and patcher (see ``eval/baseline.py`` for why);
* **paired observations** -- the same failing tests are offered to both arms,
  which is what allows a paired significance test.

The signature cache is disabled for the benchmark by default. It is a real
optimisation, but leaving it on makes a project's result depend on which
projects ran before it, and that is not a property you want in a measurement.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..cache import SignatureCache
from ..config import AutefConfig
from ..ingest import IngestError, ingest
from ..llm import LLMClient, LLMError
from ..models import ProjectLayout, RunReport
from ..orchestrator import RepairOrchestrator
from ..patcher import copy_tree
from ..venv_manager import prepare_environment
from .baseline import BaselineOrchestrator
from .faults import ALL_KINDS as ALL_FAULT_KINDS
from .faults import FaultInjector, FaultRecord
from .metrics import (
    ArmMetrics,
    compute_metrics,
    flatten_for_json,
    render_markdown,
    write_observations_csv,
)

logger = logging.getLogger(__name__)

ARMS = ("baseline", "autef2")
#: The arm under test. Coverage and mutation are measured for this one only:
#: v1 has no such stages, so running them for the baseline would report zeros
#: that read as a score rather than as an absence.
CANDIDATE = "autef2"


@dataclass
class ProjectSpec:
    """One project in the sample."""

    name: str
    source: str
    #: Stratum label, e.g. "small", "medium", "large" or a domain. Used to
    #: report results per stratum rather than as one undifferentiated pool.
    stratum: str = "unstratified"
    #: Faults to seed into this project's tests before either arm runs.
    inject: int = 0
    max_tests: Optional[int] = None
    #: Which fault kinds to seed. The mix decides the result -- the baseline
    #: cannot attempt a file-scoped failure at all -- so it belongs in the
    #: protocol rather than in whatever the injector happens to find.
    fault_kinds: Sequence[str] = ()
    #: Generate tests for this many modules before either arm runs. Generation
    #: happens once, ahead of the pristine snapshot, so both arms are offered an
    #: identical suite -- otherwise each arm would repair tests the other never
    #: saw and the comparison would not be paired.
    generate: int = 0
    #: The exact commit to measure. Without one the manifest names a moving
    #: branch, so a number quoted from a run in March cannot be reproduced in
    #: September -- tabulate went from 322 tests to 306 between two runs of
    #: this very benchmark. Recorded in the artefacts either way, so a run
    #: against a floating branch at least says what it actually measured.
    revision: Optional[str] = None
    #: Caps for the enhancement stages, when the benchmark is asked to measure
    #: them. Small by default: they are per-project costs and a benchmark runs
    #: over several.
    max_coverage_files: int = 2
    max_mutants: int = 20
    max_survivors: int = 3

    @property
    def pinned_source(self) -> str:
        """``source`` with the pinned revision applied, when there is one."""
        if not self.revision:
            return self.source
        source = self.source.rstrip("/")
        if "github.com/" in source and "/tree/" not in source:
            return f"{source}/tree/{self.revision}"
        return source

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectSpec":
        return cls(
            name=str(data.get("name") or Path(str(data["source"])).stem),
            source=str(data["source"]),
            stratum=str(data.get("stratum", "unstratified")),
            inject=int(data.get("inject", 0)),
            max_tests=data.get("max_tests"),
            generate=int(data.get("generate", 0)),
            fault_kinds=tuple(data.get("fault_kinds") or ()),
            revision=(str(data["revision"]) if data.get("revision") else None),
            max_coverage_files=int(data.get("max_coverage_files", 2)),
            max_mutants=int(data.get("max_mutants", 20)),
            max_survivors=int(data.get("max_survivors", 3)),
        )


@dataclass
class BenchmarkResult:
    reports_by_arm: Dict[str, List[RunReport]] = field(default_factory=dict)
    metrics_by_arm: Dict[str, ArmMetrics] = field(default_factory=dict)
    faults_by_project: Dict[str, List[FaultRecord]] = field(default_factory=dict)
    #: project -> why fewer faults were seeded than the spec asked for.
    fault_shortfalls: Dict[str, str] = field(default_factory=dict)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    output_dir: Optional[str] = None
    duration_s: float = 0.0
    #: What produced these numbers: model, effort, seed, framework commit and
    #: the exact source each project was measured at. Without it two runs'
    #: artefacts are indistinguishable, and no figure quoted from them can be
    #: attributed to a configuration or reproduced.
    provenance: Dict[str, object] = field(default_factory=dict)

    def markdown(self) -> str:
        return render_markdown(self.metrics_by_arm, self.reports_by_arm)


def load_specs(path: str | Path) -> List[ProjectSpec]:
    """Read a benchmark manifest (JSON, or YAML if pyyaml is installed)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PyYAML is needed for a .yaml manifest; use .json instead"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    entries = data.get("projects", data) if isinstance(data, dict) else data
    return [ProjectSpec.from_dict(entry) for entry in entries]


def stratified_sample(
    specs: Sequence[ProjectSpec], per_stratum: Optional[int], seed: int = 1337
) -> List[ProjectSpec]:
    """Take up to ``per_stratum`` projects from each stratum, deterministically."""
    if not per_stratum:
        return list(specs)
    import random

    rng = random.Random(seed)
    by_stratum: Dict[str, List[ProjectSpec]] = {}
    for spec in specs:
        by_stratum.setdefault(spec.stratum, []).append(spec)

    sample: List[ProjectSpec] = []
    for stratum in sorted(by_stratum):
        pool = list(by_stratum[stratum])
        rng.shuffle(pool)
        sample.extend(pool[:per_stratum])
    return sample


def run_benchmark(
    specs: Sequence[ProjectSpec],
    config: Optional[AutefConfig] = None,
    *,
    arms: Sequence[str] = ARMS,
    seed: int = 1337,
    output_dir: Optional[Path] = None,
    use_cache: bool = False,
    llm: Optional[LLMClient] = None,
    enhance: bool = False,
) -> BenchmarkResult:
    """Run every project through every arm.

    ``enhance`` additionally measures coverage and mutation for the candidate.
    Off by default because it multiplies the run time -- mutation re-runs the
    suite once per mutant -- and because the paired repair comparison, which is
    what the significance test is about, does not need it.
    """
    config = config or AutefConfig.from_env()
    output_dir = Path(output_dir or config.reports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = BenchmarkResult(output_dir=str(output_dir))
    result.reports_by_arm = {arm: [] for arm in arms}
    result.provenance = _provenance(config, specs, arms, seed, use_cache, enhance)
    started = time.time()

    for index, spec in enumerate(specs, start=1):
        logger.info("=== [%d/%d] %s (%s) ===", index, len(specs), spec.name, spec.stratum)
        try:
            self_reports, faults, shortfall = _run_one_project(
                spec, config, arms, seed=seed, use_cache=use_cache, llm=llm,
                enhance=enhance,
            )
        except IngestError as exc:
            logger.warning("Skipping %s: %s", spec.name, exc)
            result.skipped.append({"project": spec.name, "reason": str(exc)})
            continue
        except LLMError as exc:
            logger.error("Aborting: %s", exc)
            result.skipped.append({"project": spec.name, "reason": str(exc)})
            break

        if faults:
            result.faults_by_project[spec.name] = faults
        if shortfall:
            result.fault_shortfalls[spec.name] = shortfall
        for arm, report in self_reports.items():
            result.reports_by_arm[arm].append(report)

    result.metrics_by_arm = {
        arm: compute_metrics(arm, reports)
        for arm, reports in result.reports_by_arm.items()
    }
    result.duration_s = time.time() - started

    _write_outputs(result, output_dir)
    return result


def _run_one_project(
    spec: ProjectSpec,
    config: AutefConfig,
    arms: Sequence[str],
    *,
    seed: int,
    use_cache: bool,
    llm: Optional[LLMClient],
    enhance: bool = False,
) -> tuple[Dict[str, RunReport], List[FaultRecord], Optional[str]]:
    layout = ingest(spec.pinned_source, config, name_hint=spec.name)

    # One environment, built once and shared by both arms, so a difference
    # between them is never an artefact of dependency resolution.
    environment = prepare_environment(layout, config)

    if spec.generate:
        # Once, before the snapshot: both arms must be offered the same tests.
        from ..enhance import GenerationPhase

        client = llm or LLMClient(config)
        generation = GenerationPhase(
            layout, environment, config, client.scoped()
        ).run(max_modules=spec.generate)
        layout = generation.layout or layout
        logger.info(
            "%s: generated %d test file(s) before either arm ran",
            spec.name, len(generation.accepted),
        )

    faults: List[FaultRecord] = []
    shortfall: Optional[str] = None
    if spec.inject:
        # Establish which tests currently pass, and seed only into those. A
        # fault dropped into an already-failing test yields an observation
        # where a repair cannot be attributed to either defect.
        from ..runner import TestRunner

        probe = TestRunner(layout, environment, config).run_suite()
        if not probe.ran:
            logger.warning(
                "%s: suite would not run, so no faults were seeded", spec.name
            )
        else:
            injector = FaultInjector(
                layout,
                seed=seed,
                passing_tests=probe.passed,
                kinds=spec.fault_kinds or ALL_FAULT_KINDS,
            )
            faults = injector.inject(spec.inject)
            # Returned, not just logged: a comparison run on half the faults
            # its protocol declares is not that protocol, and the reader has to
            # be told without going to look at a log.
            shortfall = injector.shortfall

    # Pristine state, captured AFTER seeding so both arms start identically.
    pristine = Path(config.workspace) / "pristine" / _safe(spec.name)
    copy_tree(layout.root, str(pristine))

    reports: Dict[str, RunReport] = {}
    for arm in arms:
        logger.info("--- %s / %s ---", spec.name, arm)
        copy_tree(str(pristine), layout.root)

        client = llm or LLMClient(config)
        # Each arm gets its own tally so cost is attributed per arm.
        arm_llm = client.scoped()

        if arm == "baseline":
            orchestrator = BaselineOrchestrator(layout, environment, config, arm_llm)
            report = orchestrator.run(max_tests=spec.max_tests)
        elif arm == "autef2":
            cache = SignatureCache(
                config.cache_path if use_cache else None, enabled=use_cache
            )
            orchestrator = RepairOrchestrator(
                layout, environment, config, arm_llm, cache=cache
            )
            report = orchestrator.run(max_tests=spec.max_tests)
        else:
            raise ValueError(f"Unknown arm: {arm}")

        # Coverage and mutation run for the candidate only, and only when
        # asked for. v1 has no such stages -- it repairs a failing test
        # function and stops -- so running them for the baseline would report
        # zeros that read as a score rather than as an absence. What they
        # measure is what v2 adds, and the table says so.
        if arm == CANDIDATE and enhance and not report.error:
            _measure_enhancements(report, layout, environment, config, arm_llm, spec)

        report.project = spec.name
        report.arm = arm
        report.prompt_tokens = arm_llm.usage.prompt_tokens
        report.completion_tokens = arm_llm.usage.completion_tokens
        report.llm_calls = arm_llm.usage.calls
        report.cost_usd = arm_llm.usage.cost_usd
        reports[arm] = report

    # Leave the project as the last arm left it only after restoring pristine,
    # so a re-run of the benchmark is reproducible.
    copy_tree(str(pristine), layout.root)
    return reports, faults, shortfall


def _write_outputs(result: BenchmarkResult, output_dir: Path) -> None:
    (output_dir / "benchmark.md").write_text(result.markdown(), encoding="utf-8")

    payload = {
        "provenance": result.provenance,
        "metrics": flatten_for_json(result.metrics_by_arm),
        "skipped": result.skipped,
        "duration_s": round(result.duration_s, 2),
        "faults": {
            project: [f.to_dict() for f in faults]
            for project, faults in result.faults_by_project.items()
        },
        "reports": {
            arm: [r.to_dict() for r in reports]
            for arm, reports in result.reports_by_arm.items()
        },
    }
    (output_dir / "benchmark.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )

    write_observations_csv(output_dir / "observations.csv", result.reports_by_arm)
    logger.info("Wrote benchmark.md, benchmark.json, observations.csv to %s", output_dir)


def _measure_enhancements(report, layout, environment, config, llm, spec) -> None:
    """Coverage and mutation for one project, onto an existing report.

    Runs after repair so the suite is as green as the arm could make it: a
    mutation score taken against a suite the arm has not finished repairing
    measures the wrong thing, and the coverage figure would be the one the
    project arrived with.
    """
    from ..enhance import CoveragePhase, MutationPhase

    try:
        coverage = CoveragePhase(layout, environment, config, llm).run(
            max_files=spec.max_coverage_files
        )
        report.coverage_before = coverage.before
        report.coverage_after = coverage.after
        report.coverage_generated = coverage.records
        if coverage.skipped_reason:
            report.stage_skips["coverage"] = coverage.skipped_reason
    except Exception as exc:  # noqa: BLE001 - one project must not end the run
        logger.warning("%s: coverage failed: %s", spec.name, exc)
        report.stage_skips["coverage"] = str(exc)

    try:
        mutation = MutationPhase(layout, environment, config, llm).run(
            max_mutants=spec.max_mutants, max_survivors=spec.max_survivors,
        )
        report.mutation_before = mutation.before
        report.mutation_after = mutation.after
        report.mutation_generated = mutation.records
        if mutation.skipped_reason:
            report.stage_skips["mutation"] = mutation.skipped_reason
        if mutation.excluded:
            report.stage_skips["mutation_excluded"] = (
                f"{len(mutation.excluded)} already-failing test(s) excluded"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: mutation failed: %s", spec.name, exc)
        report.stage_skips["mutation"] = str(exc)


def _provenance(config, specs, arms, seed, use_cache, enhance=False) -> Dict[str, object]:
    """What produced these numbers.

    Two runs' artefacts were previously indistinguishable: nothing recorded the
    model, the seed, or which commit of each project was measured, so no figure
    quoted from them could be attributed to a configuration or reproduced. The
    model matters more than it used to -- a reasoning model refuses
    temperature 0, so runs are not bit-identical and the configuration is the
    only thing that can be pinned.
    """
    return {
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "temperature_requested": config.temperature,
        "max_attempts": config.max_attempts,
        "seed": seed,
        "arms": list(arms),
        "signature_cache": bool(use_cache),
        "enhancements_measured": bool(enhance),
        "autef2_commit": _framework_commit(),
        "projects": [
            {
                "name": spec.name,
                "source": spec.source,
                "revision": spec.revision,
                "measured_source": spec.pinned_source,
                "pinned": bool(spec.revision),
                "stratum": spec.stratum,
                "inject": spec.inject,
                "fault_kinds": list(spec.fault_kinds),
            }
            for spec in specs
        ],
        "unpinned_projects": [s.name for s in specs if not s.revision],
    }


def _framework_commit() -> Optional[str]:
    """The commit of AUTEF itself, when it is running from a checkout."""
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60]
