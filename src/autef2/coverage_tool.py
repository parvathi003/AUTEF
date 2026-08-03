"""Measure line and branch coverage of the project under test.

v1's CoverageAgent measured coverage of *itself*. It created a
``coverage.Coverage`` object at import time, inside a Streamlit
``@st.cache_resource``, in AUTEF's own interpreter:

    cov = coverage.Coverage(include=["*/source_files/*"], branch=True)
    cov.start()

Three problems follow from that. The ``include`` pattern hardcodes one directory
name. The measurement happens in the host process, so it can only see code the
host imports -- which for an uploaded project means whatever ``unittest.discover``
managed to load, in the host's dependency set rather than the project's. And the
coverage object is a cached singleton, so a second project measured in the same
session reports the first one's data.

Here coverage runs as a subprocess in the project's own virtualenv, over the
same pytest invocation the suite runs under, with ``--source`` taken from the
detected source roots. The result is read from ``coverage json``, which gives
per-file missing lines and missing branches -- the input the coverage-improvement
prompt actually needs.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence

from .config import AutefConfig
from .models import CoverageSnapshot, FileCoverage, ProjectLayout
from .runner import TestRunner
from .venv_manager import Environment, install_requirement

logger = logging.getLogger(__name__)

#: Coverage is only needed when coverage is asked for, so it is not part of the
#: standard runner requirements.
COVERAGE_REQUIREMENT = "coverage>=7.0"


class CoverageTool:
    """Runs the suite under coverage and reports what it did not reach."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.runner = TestRunner(layout, environment, config)
        self._data_dir = Path(config.workspace) / "coverage"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # -- public API -------------------------------------------------------

    def available(self) -> bool:
        """Is ``coverage`` importable by the interpreter that runs the tests?"""
        try:
            completed = subprocess.run(
                [self.environment.python, "-c", "import coverage"],
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    def ensure_available(self) -> bool:
        if self.available():
            return True
        logger.info("Installing coverage into the project environment")
        install_requirement(self.environment, COVERAGE_REQUIREMENT, self.config)
        return self.available()

    def measure(self, *, targets: Optional[Sequence[str]] = None) -> CoverageSnapshot:
        """Run the suite under coverage and return the snapshot."""
        started = time.time()
        snapshot = CoverageSnapshot()

        if not self.ensure_available():
            snapshot.error = (
                "coverage could not be installed into the project environment, "
                "so coverage cannot be measured"
            )
            snapshot.duration_s = time.time() - started
            return snapshot

        sources = self._sources()
        if not sources:
            snapshot.error = "no source roots were detected to measure"
            snapshot.duration_s = time.time() - started
            return snapshot

        run_id = uuid.uuid4().hex
        data_file = self._data_dir / f".coverage.{run_id}"
        json_file = self._data_dir / f"coverage.{run_id}.json"

        arguments = self.runner.pytest_arguments(
            list(targets) if targets else self.runner.default_targets()
        )
        command = [
            self.environment.python, "-m", "coverage", "run",
            f"--data-file={data_file}",
            "--branch",
            f"--source={','.join(sources)}",
            "-m", "pytest", *arguments,
        ]

        run = self._exec(command, timeout=self.config.suite_timeout_s)
        if run is None:
            snapshot.error = "the coverage run could not be started"
            snapshot.duration_s = time.time() - started
            return snapshot

        if not data_file.exists() and not self._combine_parallel(data_file):
            snapshot.error = (
                "the coverage run produced no data. "
                + (run.stdout or run.stderr or "")[-400:]
            )
            snapshot.duration_s = time.time() - started
            return snapshot

        report = self._exec(
            [
                self.environment.python, "-m", "coverage", "json",
                f"--data-file={data_file}", "-o", str(json_file),
                "--show-contexts",
            ],
            timeout=300,
        )
        if report is None or not json_file.exists():
            snapshot.error = (
                "coverage json failed: "
                + ((report.stderr or report.stdout)[-300:] if report else "not started")
            )
            snapshot.duration_s = time.time() - started
            return snapshot

        try:
            data = json.loads(json_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            snapshot.error = f"coverage json could not be read: {exc}"
            snapshot.duration_s = time.time() - started
            return snapshot

        _fill(snapshot, data, self.layout)
        snapshot.measured = True
        snapshot.duration_s = time.time() - started
        logger.info(
            "coverage: %.1f%% of %d statements, %.1f%% of %d branches",
            snapshot.line_rate * 100, snapshot.statements,
            snapshot.branch_rate * 100, snapshot.branches,
        )
        return snapshot

    # -- internals --------------------------------------------------------

    def _combine_parallel(self, data_file: Path) -> bool:
        """Fold coverage's parallel-mode fragments into the file we asked for.

        A project whose own coverage config sets ``parallel = true`` -- common,
        so a CI matrix can merge runs -- makes coverage ignore the data-file
        name and write ``<name>.<host>.<pid>.<random>`` instead. We run in the
        project root, so the project's config is the one in force. The
        measurement is there; only the name is not the one we asked for, and
        reporting "produced no data" for a run that measured the whole suite is
        the wrong conclusion from the right observation.
        """
        fragments = sorted(data_file.parent.glob(data_file.name + ".*"))
        if not fragments:
            return False

        logger.info(
            "coverage ran in parallel mode; combining %d fragment(s)",
            len(fragments),
        )
        self._exec(
            [
                self.environment.python, "-m", "coverage", "combine",
                f"--data-file={data_file}",
                *(str(fragment) for fragment in fragments),
            ],
            timeout=300,
        )
        return data_file.exists()

    def _sources(self) -> List[str]:
        """What to measure: the detected source roots, as relative paths.

        Relative because coverage resolves them against the run's working
        directory, which is the project root -- and because a relative source
        keeps the reported file paths relative too, which is what the prompt and
        the report want to show.
        """
        roots = self.layout.source_roots or [self.layout.root]
        project = Path(self.layout.root).resolve()
        relative: List[str] = []
        for root in roots:
            path = Path(root).resolve()
            try:
                text = str(path.relative_to(project))
            except ValueError:
                continue
            relative.append(text or ".")
        return relative or ["."]

    def _exec(self, command: Sequence[str], *, timeout: int):
        try:
            return subprocess.run(
                command,
                cwd=self.layout.root,
                env=self.runner.subprocess_env(),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                # This runs the project's suite too, so it inherits the same
                # hazard: a test that reads stdin would block until the
                # timeout. See ``TestRunner._run``.
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("coverage command failed: %s", exc)
            return None


def _fill(
    snapshot: CoverageSnapshot, data: dict, layout: ProjectLayout
) -> None:
    """Read coverage's JSON into the snapshot.

    Field names are read defensively: the JSON schema has gained keys across
    coverage releases, and the project's own pinned version decides which are
    present.
    """
    files = data.get("files") or {}
    project = Path(layout.root).resolve()

    for raw_path, entry in sorted(files.items()):
        summary = entry.get("summary") or {}
        path = Path(raw_path)
        if not path.is_absolute():
            path = project / path

        file_coverage = FileCoverage(
            path=str(path),
            statements=int(summary.get("num_statements") or 0),
            covered_statements=int(summary.get("covered_lines") or 0),
            missing_lines=[int(n) for n in (entry.get("missing_lines") or [])],
            branches=int(summary.get("num_branches") or 0),
            covered_branches=int(summary.get("covered_branches") or 0),
        )

        for branch in entry.get("missing_branches") or []:
            if isinstance(branch, (list, tuple)) and len(branch) >= 2:
                file_coverage.missing_branches.append([int(branch[0]), int(branch[1])])

        snapshot.files.append(file_coverage)
        snapshot.statements += file_coverage.statements
        snapshot.covered_statements += file_coverage.covered_statements
        snapshot.branches += file_coverage.branches
        snapshot.covered_branches += file_coverage.covered_branches

    totals = data.get("totals") or {}
    # Prefer coverage's own totals when it reports them; they account for
    # exclusions the per-file sums do not.
    if totals.get("num_statements"):
        snapshot.statements = int(totals["num_statements"])
        snapshot.covered_statements = int(totals.get("covered_lines") or 0)
    if totals.get("num_branches"):
        snapshot.branches = int(totals["num_branches"])
        snapshot.covered_branches = int(totals.get("covered_branches") or 0)


def gaps(snapshot: CoverageSnapshot, *, limit: Optional[int] = None) -> List[FileCoverage]:
    """Files with something uncovered, worst first.

    Ordered by how much is missing rather than by percentage, so a large file
    with 200 unreached statements outranks a three-line module at 0%.
    """
    incomplete = [
        f for f in snapshot.files if f.missing_lines or f.missing_branches
    ]
    incomplete.sort(
        key=lambda f: (len(f.missing_lines) + len(f.missing_branches)), reverse=True
    )
    return incomplete[:limit] if limit else incomplete
