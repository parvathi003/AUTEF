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
) -> BenchmarkResult:
    config = config or AutefConfig.from_env()
    output_dir = Path(output_dir or config.reports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = BenchmarkResult(output_dir=str(output_dir))
    result.reports_by_arm = {arm: [] for arm in arms}
    started = time.time()

    for index, spec in enumerate(specs, start=1):
        logger.info("=== [%d/%d] %s (%s) ===", index, len(specs), spec.name, spec.stratum)
        try:
            self_reports, faults, shortfall = _run_one_project(
                spec, config, arms, seed=seed, use_cache=use_cache, llm=llm
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
) -> tuple[Dict[str, RunReport], List[FaultRecord], Optional[str]]:
    layout = ingest(spec.source, config, name_hint=spec.name)

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


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60]
