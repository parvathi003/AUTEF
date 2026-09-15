"""Run a project's tests under pytest and parse structured results.

v1 ran ``unittest.defaultTestLoader.discover`` with a hardcoded
``top_level_dir``, which meant a project using pytest-style function tests,
fixtures or parametrisation reported zero tests found. pytest collects
unittest.TestCase classes too, so moving to pytest is a strict widening: every
suite v1 could run still runs, plus the majority of real projects that v1
could not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import AutefConfig
from .models import Frame, Outcome, ProjectLayout, SuiteResult, TestFailure
from .venv_manager import Environment

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent / "_plugin"
PLUGIN_NAME = "autef_report"

#: pytest exit status 5 means "no tests collected" -- not an error for us.
EXIT_NO_TESTS = 5


#: Environment variables never passed to a project's test subprocess. The
#: suite being run is arbitrary code from an uploaded repository -- a conftest
#: that reads os.environ is entirely ordinary -- and it used to inherit the
#: operator's whole environment, OPENAI_API_KEY included. Nothing a unit test
#: legitimately needs is in here.
_SECRET_NAME_RE = re.compile(
    r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY"
    r"|SESSION[_-]?KEY|AUTH)", re.I
)
#: Stripped by name as well, since these do not all match the pattern.
_SECRET_NAMES = frozenset({
    "OPENAI_API_KEY", "OPENAI_ORGANIZATION", "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "AUTEF_USER",
    "AUTEF_PASSWORD",
})


def _without_secrets(source) -> Dict[str, str]:
    """A copy of the environment with credentials removed."""
    env: Dict[str, str] = {}
    dropped = []
    for name, value in source.items():
        if name in _SECRET_NAMES or _SECRET_NAME_RE.search(name):
            dropped.append(name)
            continue
        env[name] = value
    if dropped:
        logger.debug("Withheld from the test subprocess: %s", ", ".join(sorted(dropped)))
    return env


class TestRunner:
    """Executes pytest against one project and returns parsed results."""

    #: Stops pytest trying to collect this as a test class.
    __test__ = False

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        *,
        strip_addopts: bool = True,
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        # Projects routinely set addopts like "--cov=pkg -n auto" in their
        # pytest.ini. If the matching plugin is not installed, pytest aborts on
        # a usage error before running a single test. Neutralising addopts
        # makes far more projects runnable; --keep-addopts restores them.
        self.strip_addopts = strip_addopts
        self._report_dir = Path(config.workspace) / "pytest_reports"
        self._report_dir.mkdir(parents=True, exist_ok=True)

    # -- public API -------------------------------------------------------

    def run_suite(self, targets: Optional[Sequence[str]] = None) -> SuiteResult:
        """Run the whole suite (or the given targets).

        Multiple test roots are run one at a time rather than in a single pytest
        invocation. A repository often carries test roots it cannot itself run
        -- Flask ships ``examples/*/tests`` for separate demo projects that are
        not installed -- and in one invocation a single unimportable conftest
        aborts collection for everything, so a project with 400 working tests
        reports none at all.
        """
        if targets is not None:
            return self._run(list(targets), timeout=self.config.suite_timeout_s)

        roots = self._default_targets()
        if len(roots) <= 1:
            return self._run(roots, timeout=self.config.suite_timeout_s)

        merged = SuiteResult(ran=False)
        for root in roots:
            result = self._run([root], timeout=self.config.suite_timeout_s)
            _merge_into(merged, result, root)
        logger.info(
            "%d test roots -> %d passed, %d failed, %d collection errors",
            len(roots), len(merged.passed), len(merged.failures),
            len(merged.collection_errors),
        )
        return merged

    def run_node(self, nodeid: str) -> SuiteResult:
        """Run exactly one test. Used to verify a repair."""
        return self._run([nodeid], timeout=self.config.single_test_timeout_s)

    def run_fail_fast(self, targets: Optional[Sequence[str]] = None) -> SuiteResult:
        """Stop at the first failure. Used when only pass/fail matters.

        Mutation testing asks one question per mutant -- did anything notice? --
        and a mutant that is caught by the first test does not need the other
        four hundred run. Every root goes into one invocation here, unlike
        ``run_suite``, because a single verdict is wanted rather than a full
        picture of the suite.
        """
        args = list(targets) if targets else self._default_targets()
        return self._run(
            [*args, "-x"], timeout=self.config.mutant_timeout_s
        )

    def run_file(self, test_file: str) -> SuiteResult:
        """Run one test file. Used to check a repair broke nothing nearby."""
        relative = self._relative(test_file)
        return self._run([relative], timeout=self.config.single_test_timeout_s * 4)

    # -- execution --------------------------------------------------------

    def _default_targets(self) -> List[str]:
        roots = self.layout.test_roots or [self.layout.root]
        return [self._relative(r) for r in roots]

    def _python_files(self) -> Optional[str]:
        """Widen pytest's collection patterns, but only when the project needs it.

        Overriding ``python_files`` replaces whatever the project configured, so
        it is done only for projects that actually carry ``tests.py`` or
        ``test.py`` -- Django apps, mostly. Everything else keeps pytest's own
        behaviour.
        """
        from .ingest import NON_STANDARD_TEST_FILES

        found = {
            Path(f).name
            for f in self.layout.test_files
            if Path(f).name in NON_STANDARD_TEST_FILES
        }
        if not found:
            return None
        return " ".join(["test_*.py", "*_test.py", *sorted(found)])

    def _relative(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path(self.layout.root).resolve()))
        except ValueError:
            return str(path)

    def default_targets(self) -> List[str]:
        """The test roots, relative to the project. Public for the coverage run."""
        return self._default_targets()

    def pytest_arguments(self, targets: Sequence[str]) -> List[str]:
        """Everything after ``-m pytest``.

        Shared with the coverage run so the two cannot drift: measuring coverage
        over a different set of flags than the suite runs under would measure a
        different suite.
        """
        args = [
            *targets,
            "-p", PLUGIN_NAME,
            "-p", "no:cacheprovider",
            "--tb=long",
            "-q",
            "--continue-on-collection-errors",
        ]
        if self.strip_addopts:
            args += ["-o", "addopts="]
        widened = self._python_files()
        if widened:
            args += ["-o", f"python_files={widened}"]
        return args

    def subprocess_env(self, report_path: Optional[Path] = None) -> Dict[str, str]:
        """The environment a test subprocess needs. Public for the same reason."""
        return self._build_env(report_path)

    def _run(self, args: Sequence[str], *, timeout: int) -> SuiteResult:
        report_path = self._report_dir / f"report_{uuid.uuid4().hex}.jsonl"
        command = [
            self.environment.python,
            "-m", "pytest",
            *self.pytest_arguments(args),
        ]

        env = self.subprocess_env(report_path)
        started = time.time()
        try:
            completed = subprocess.run(
                command,
                cwd=self.layout.root,
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                # Without this the tests inherit our stdin. Any test that reads
                # it then blocks until the timeout and takes the whole suite
                # with it -- click's suite exercises prompt/confirm/getchar and
                # stalls at about 70%. Closed stdin gives such a test EOF, so it
                # fails in milliseconds, which is a repairable failure rather
                # than a fifteen-minute hang.
                stdin=subprocess.DEVNULL,
            )
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            stderr = _as_text(exc.stderr)
            returncode = -1
            timed_out = True
        except OSError as exc:
            return SuiteResult(
                ran=False,
                returncode=-1,
                stdout_tail=f"pytest could not be started: {exc}",
                duration_s=time.time() - started,
            )

        duration = time.time() - started
        result = self._parse(report_path)
        result.duration_s = duration
        result.returncode = returncode
        result.stdout_tail = _tail(stdout + "\n" + stderr, 4000)

        if timed_out:
            result.ran = False
            result.timed_out = True
            result.stdout_tail = f"[timed out after {timeout}s]\n" + result.stdout_tail
        elif result.reported == 0 and returncode not in (0, EXIT_NO_TESTS):
            # pytest failed before collecting anything (usage error, import
            # crash at conftest level). Surface it rather than reporting a
            # clean empty run.
            result.ran = False

        logger.info(
            "pytest %s -> %d passed, %d failed, %d skipped, %d collection errors "
            "(rc=%s, %.1fs)",
            " ".join(args), len(result.passed), len(result.failures),
            len(result.skipped), len(result.collection_errors), returncode, duration,
        )
        return result

    def _build_env(self, report_path: Optional[Path]) -> Dict[str, str]:
        env = _without_secrets(os.environ)
        if report_path is not None:
            env["AUTEF_REPORT_PATH"] = str(report_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Deterministic hashing keeps set/dict ordering stable between the
        # pre-repair and post-repair runs, so a "regression" is a real one.
        env["PYTHONHASHSEED"] = "0"

        path_entries = [str(PLUGIN_DIR), *self.layout.import_roots]
        existing = env.get("PYTHONPATH", "")
        if existing:
            path_entries.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(path_entries)
        return env

    # -- parsing ----------------------------------------------------------

    def _parse(self, report_path: Path) -> SuiteResult:
        result = SuiteResult()
        if not report_path.exists():
            return result

        for line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = record.get("kind")
            if kind == "session":
                continue
            if kind == "collect":
                result.collection_errors.append(
                    self._to_failure(record, Outcome.ERROR)
                )
                continue

            outcome = record.get("outcome")
            nodeid = record.get("nodeid", "")
            if outcome == "passed":
                result.passed.append(nodeid)
            elif outcome == "skipped":
                result.skipped.append(nodeid)
            elif outcome == "failed":
                phase = record.get("when", "call")
                # A setup-phase failure is an error, not an assertion failure;
                # the distinction feeds the root-cause taxonomy.
                kind_outcome = Outcome.FAILED if phase == "call" else Outcome.ERROR
                result.failures.append(self._to_failure(record, kind_outcome))

        return result

    def _to_failure(self, record: dict, outcome: Outcome) -> TestFailure:
        crash = record.get("crash") or {}
        message = str(crash.get("message", "") or "")
        exception_type, exception_message = _split_exception(message)

        frames = [
            Frame(
                path=str(f.get("path", "")),
                lineno=int(f.get("lineno", 0) or 0),
                message=str(f.get("message", "")),
            )
            for f in record.get("frames") or []
        ]
        if not frames and crash.get("path"):
            frames = [
                Frame(
                    path=str(crash["path"]),
                    lineno=int(crash.get("lineno", 0) or 0),
                    message=message,
                )
            ]

        longrepr = str(record.get("longrepr", "") or "")
        if not exception_type or not frames:
            # A collection failure carries no reprcrash and no reprentries --
            # pytest only renders it as text. Everything the resolver and the
            # diagnosis need is in that text, so read it from there rather than
            # letting an import crash arrive with no cause and no location.
            text_type, text_message, text_frames = _from_longrepr(longrepr)
            if not exception_type and text_type:
                exception_type, exception_message = text_type, text_message
            if not frames and text_frames:
                frames = text_frames

        return TestFailure(
            nodeid=record.get("nodeid", ""),
            outcome=outcome,
            exception_type=exception_type,
            exception_message=exception_message,
            longrepr=_tail(longrepr, self.config.max_traceback_chars),
            frames=frames,
            phase=str(record.get("when", "call")),
            duration=float(record.get("duration", 0.0) or 0.0),
        )


def _merge_into(merged: SuiteResult, result: SuiteResult, root: str) -> None:
    """Fold one test root's result into the whole-project result.

    ``ran`` is true if *any* root ran: one root the project cannot import must
    not erase the roots that work. A root that failed to start is recorded in
    the output tail so the reason is still visible.
    """
    merged.passed.extend(result.passed)
    merged.failures.extend(result.failures)
    merged.skipped.extend(result.skipped)
    merged.collection_errors.extend(result.collection_errors)
    merged.duration_s += result.duration_s
    # One root hanging is worth saying even when another root ran fine: the
    # numbers are then a partial suite, not the project's.
    merged.timed_out = merged.timed_out or result.timed_out

    if result.ran:
        merged.ran = True
        if result.returncode not in (0, EXIT_NO_TESTS) and merged.returncode == 0:
            merged.returncode = result.returncode
        return

    logger.warning("Test root %s could not be executed; continuing", root)
    merged.stdout_tail = (
        f"{merged.stdout_tail}\n[test root {root!r} could not be executed]\n"
        f"{result.stdout_tail}"
    ).strip()
    if merged.returncode == 0:
        merged.returncode = result.returncode


#: ``tests\test_x.py:10: in <module>`` -- pytest's long traceback frame header.
_FRAME_RE = re.compile(r"^(?P<path>[^\s].*?\.py):(?P<lineno>\d+): in (?P<func>.+)$")

#: ``E   ModuleNotFoundError: No module named 'calc.operations'``
_ERROR_LINE_RE = re.compile(r"^E\s+(?P<body>\S.*)$")


def _from_longrepr(text: str) -> tuple[str, str, List[Frame]]:
    """Recover (exception type, message, frames) from a rendered traceback.

    Used for collection failures, where pytest exposes no structured crash or
    traceback entries -- only the text it would print. Without this an import
    error reaches the diagnosis with an empty message, which also makes every
    such failure hash to the same signature.
    """
    if not text:
        return "", "", []

    frames: List[Frame] = []
    error_bodies: List[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        match = _FRAME_RE.match(line.strip())
        if match:
            frames.append(
                Frame(
                    path=match.group("path"),
                    lineno=int(match.group("lineno")),
                    message=match.group("func").strip(),
                )
            )
            continue
        error = _ERROR_LINE_RE.match(line.strip())
        if error:
            error_bodies.append(error.group("body").strip())

    # The last E-line is the exception that actually propagated; earlier ones
    # belong to the source echo or to a chained cause.
    exception_type, exception_message = "", ""
    for body in reversed(error_bodies):
        exception_type, exception_message = _split_exception(body)
        if exception_type:
            break
    if not exception_type and error_bodies:
        exception_message = error_bodies[-1]

    return exception_type, exception_message, frames


_EXC_RE = re.compile(r"^\s*(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Warning|Failure|Exit))\b\s*:?\s*(.*)", re.DOTALL)


def _split_exception(message: str) -> tuple[str, str]:
    """Split 'AssertionError: assert 1 == 2' into type and message."""
    text = (message or "").strip()
    if not text:
        return "", ""
    match = _EXC_RE.match(text)
    if match:
        return match.group(1), match.group(2).strip()
    # Bare assert rewriting: pytest reports "assert 1 == 2" with no type.
    if text.startswith("assert"):
        return "AssertionError", text
    head, _, tail = text.partition(":")
    if head and " " not in head.strip():
        return head.strip(), tail.strip()
    return "", text


def _tail(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
