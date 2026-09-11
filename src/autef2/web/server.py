"""HTTP server for the AUTEF v2 web front end.

    POST /api/login      username + password -> session token
    POST /api/upload     raw .zip body, stored for stage 1
    POST /api/config     run settings for this session
    POST /api/stage      start one stage (1..9) in the background
    POST /api/run-all    chain every stage
    GET  /api/state      poll: which stage is running, and every result so far
    POST /api/reset      drop the session's pipeline state
    POST /api/logout

Stages take minutes, so a stage request starts a worker thread and returns
immediately; the page polls ``/api/state``. One session is one pipeline: the
detected layout, the virtualenv and the model client all live as long as the
browser session does.

Deliberately built on ``http.server`` from the standard library. Adding Flask or
FastAPI would put a second web framework into a project whose whole argument is
about dependency portability, to serve one page and eight endpoints.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000
STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Hardcoded access, as specified. Override with AUTEF_USER / AUTEF_PASSWORD.
#:
#: This is a demonstration gate, not a security boundary: the credential is in
#: the source, the transport is plain HTTP, and anything the pipeline can do to
#: the machine, a signed-in user can do. Bind to localhost or a trusted network.
DEFAULT_USERNAME = os.environ.get("AUTEF_USER", "autef")
DEFAULT_PASSWORD = os.environ.get("AUTEF_PASSWORD", "autef2025")

STAGE_NAMES = {
    1: "Ingest",
    2: "Environment",
    3: "Run suite",
    4: "Generate tests",
    5: "Diagnose",
    6: "Repair & verify",
    7: "Coverage",
    8: "Mutation",
    9: "Report",
}

#: Stages that call the model, and therefore cost money.
BILLED_STAGES = {4, 5, 6, 7, 8}


class StageError(RuntimeError):
    """A stage refused to proceed, with a reason meant for the user."""


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


class LogCollector(logging.Handler):
    """Feeds the framework's own log lines to the browser."""

    def __init__(self, sink: List[str], limit: int = 400):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.limit = limit

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - I/O
        try:
            self.sink.append(f"{record.levelname:<7} {record.getMessage()}")
            if len(self.sink) > self.limit:
                del self.sink[: len(self.sink) - self.limit]
        except Exception:
            pass


class Session:
    """One signed-in browser, and the pipeline state belonging to it."""

    def __init__(self, username: str):
        self.username = username
        self.created = time.time()
        self.lock = threading.Lock()
        self.logs: List[str] = []
        self.settings: Dict[str, Any] = {
            "model": "gpt-5",
            "reasoning_effort": "medium",
            "max_attempts": 3,
            "use_venv": False,
            "use_cache": True,
            "max_tests": 0,
            "max_modules": 5,
            "max_coverage_files": 3,
            "max_mutants": 20,
            "max_survivors": 5,
        }
        self.source: Optional[str] = None
        self.source_label: str = ""
        self.workspace: Optional[Path] = None

        self.running: Optional[int] = None
        self.queue: List[int] = []
        self.error: Optional[str] = None
        self.elapsed = 0.0
        self.state: Dict[str, Any] = {}
        self.done: Dict[int, bool] = {}

    def reset_pipeline(self) -> None:
        # The model client survives: its token and cost tally is for the
        # session, not for one project.
        for key in list(self.state):
            if key != "llm":
                self.state.pop(key, None)
        self.done.clear()
        self.logs.clear()
        self.error = None
        self.elapsed = 0.0
        self.queue = []

    def reset_from(self, stage: int) -> None:
        """Drop everything a later stage derived, so the page cannot lie."""
        order = {
            1: ("layout", "environment", "before", "failures", "records", "after",
                "orchestrator", "baseline_passing", "failing_source", "generation",
                "coverage",
                "mutation"),
            2: ("environment", "before", "failures", "records", "after",
                "orchestrator", "baseline_passing", "failing_source", "generation",
                "coverage",
                "mutation"),
            3: ("before", "failures", "records", "after", "baseline_passing",
                "failing_source",
                "generation", "coverage", "mutation"),
            # Stage 4 keeps ``before``: it describes the project as it arrived,
            # and generation adds files after that snapshot was taken.
            4: ("generation", "failures", "records", "after", "baseline_passing",
                "failing_source",
                "coverage", "mutation"),
            5: ("records", "failing_source", "after"),
            6: ("after",),
            7: ("coverage", "coverage_repaired", "mutation", "after"),
            8: ("mutation", "after"),
            9: (),
        }
        for key in order.get(stage, ()):
            self.state.pop(key, None)
        for later in range(stage, 10):
            self.done.pop(later, None)
        self.error = None


SESSIONS: Dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# pipeline stages
# ---------------------------------------------------------------------------


def _config(session: Session):
    from ..config import AutefConfig

    s = session.settings
    if session.workspace is None:
        session.workspace = AutefConfig().workspace
    return AutefConfig.from_env(
        workspace=session.workspace,
        model=s["model"],
        reasoning_effort=s["reasoning_effort"] or None,
        max_attempts=int(s["max_attempts"]),
        use_venv=bool(s["use_venv"]),
        use_signature_cache=bool(s["use_cache"]),
    )


def _llm(session: Session, config):
    from ..llm import LLMClient

    if "llm" not in session.state:
        session.state["llm"] = LLMClient(config)
    return session.state["llm"]


def _collect_suite(session: Session, config, *, baseline: bool) -> None:
    """Run the suite and refresh the failures to work from.

    ``before`` is written on the baseline pass only. Stages 4 and 7 add test
    files and call this again; a later run must not rewrite the snapshot that
    describes the project as it arrived.
    """
    from ..resolver import resolve_all
    from ..runner import TestRunner

    st = session.state
    result = TestRunner(st["layout"], st["environment"], config).run_suite()
    if baseline:
        st["before"] = result

    if not result.ran:
        if result.timed_out:
            raise StageError(
                f"The suite was still running after {config.suite_timeout_s}s "
                "and was stopped, so its result is incomplete. Usually one test "
                "is blocking. Raise the timeout, or choose a smaller project."
            )
        raise StageError(
            "The test suite could not be executed, so this project is out of "
            "scope for repair. " + result.stdout_tail[-400:]
        )

    failures = resolve_all(
        list(result.failures) + list(result.collection_errors), st["layout"]
    )
    limit = int(session.settings["max_tests"] or 0)
    if limit:
        failures = failures[:limit]
    st["failures"] = failures
    st["baseline_passing"] = list(result.passed)


def _stage_ingest(session: Session, config) -> None:
    from ..ingest import IngestError, ingest

    session.reset_from(1)
    if not session.source:
        raise StageError("Choose a project first.")
    try:
        st_layout = ingest(
            session.source, config, name_hint=session.source_label or None
        )
    except IngestError as exc:
        raise StageError(str(exc)) from exc
    session.state["layout"] = st_layout


def _stage_environment(session: Session, config) -> None:
    from ..venv_manager import prepare_environment

    session.reset_from(2)
    session.state["environment"] = prepare_environment(
        session.state["layout"], config
    )


def _stage_run_suite(session: Session, config) -> None:
    session.reset_from(3)
    _collect_suite(session, config, baseline=True)


def _stage_generate(session: Session, config) -> None:
    """Write tests for modules that have none, then re-collect.

    Placed before diagnosis on purpose: a generated test that fails is exactly
    what the repair loop exists for, so generation feeds stages 5 and 6 rather
    than sitting beside them.
    """
    from ..enhance import GenerationPhase

    session.reset_from(4)
    st = session.state
    outcome = GenerationPhase(
        st["layout"], st["environment"], config, _llm(session, config)
    ).run(max_modules=int(session.settings["max_modules"]))
    st["generation"] = outcome
    if outcome.layout is not None:
        st["layout"] = outcome.layout
    st.pop("orchestrator", None)
    _collect_suite(session, config, baseline=False)


def _orchestrator(session: Session, config):
    from ..orchestrator import RepairOrchestrator

    st = session.state
    if "orchestrator" not in st:
        st["orchestrator"] = RepairOrchestrator(
            st["layout"], st["environment"], config, _llm(session, config)
        )
    return st["orchestrator"]


def _capture_failing_source(failure) -> Optional[Dict[str, Any]]:
    """The test function as it stood when it failed, and the line that broke.

    Captured at diagnosis, not at render time: by the time the page asks, the
    repair loop may have rewritten the function, and showing the repaired code
    next to the original error would be actively misleading.
    """
    from ..patcher import find_function

    if not failure.test_file or not failure.test_function:
        return None
    try:
        span = find_function(
            failure.test_file, failure.test_function, failure.test_class
        )
    except Exception:  # pragma: no cover - a file we cannot parse
        span = None
    if span is None:
        return None

    # The deepest frame pointing at the test file is where it went wrong;
    # frames below that are inside the library under test.
    #
    # pytest reports frame paths relative to the project root ("tests\\x.py")
    # while the resolver stores an absolute one, so comparing them directly
    # never matches. Compare on the tail of the path instead, and require the
    # line to fall inside the function we are showing.
    error_line = None
    wanted = PurePath(str(failure.test_file)).name
    for frame in failure.frames:
        if PurePath(str(frame.path).replace("\\", "/")).name != wanted:
            continue
        if span.start_line <= frame.lineno <= span.end_line:
            error_line = frame.lineno

    return {
        "code": span.source,
        "start_line": span.start_line,
        "error_line": error_line,
        "exception": failure.exception_type,
        "message": (failure.exception_message or "")[:400],
        "file": Path(failure.test_file).name,
    }


def _stage_diagnose(session: Session, config) -> None:
    session.reset_from(5)
    st = session.state
    orchestrator = _orchestrator(session, config)
    records = {}
    sources = {}
    for failure in st.get("failures") or []:
        records[failure.nodeid] = orchestrator.diagnose(failure)
        captured = _capture_failing_source(failure)
        if captured:
            sources[failure.nodeid] = captured
    st["records"] = records
    st["failing_source"] = sources
    st.pop("coverage_repaired", None)


def _stage_repair(session: Session, config) -> None:
    session.reset_from(6)
    st = session.state
    orchestrator = _orchestrator(session, config)
    records = st.get("records") or {}
    baseline_passing = st.get("baseline_passing") or []
    for failure in st.get("failures") or []:
        records[failure.nodeid] = orchestrator.repair(
            failure, baseline_passing, record=records.get(failure.nodeid)
        )
    st["records"] = records


def _repair_pass(session: Session, config, *, merge: bool) -> None:
    """Diagnose and repair everything currently failing.

    ``merge`` keeps records from an earlier pass. Stage 6 replaces them; a
    later pass over newly written tests adds to them, because the earlier
    repairs are still part of the run and must survive into the report.
    """
    st = session.state
    orchestrator = _orchestrator(session, config)
    records = dict(st.get("records") or {}) if merge else {}
    sources = dict(st.get("failing_source") or {}) if merge else {}
    baseline_passing = st.get("baseline_passing") or []
    for failure in st.get("failures") or []:
        diagnosed = orchestrator.diagnose(failure)
        captured = _capture_failing_source(failure)
        if captured:
            sources[failure.nodeid] = captured
        records[failure.nodeid] = orchestrator.repair(
            failure, baseline_passing, record=diagnosed
        )
    st["records"] = records
    st["failing_source"] = sources


def _stage_coverage(session: Session, config) -> None:
    from ..enhance import CoveragePhase

    session.reset_from(7)
    st = session.state
    outcome = CoveragePhase(
        st["layout"], st["environment"], config, _llm(session, config)
    ).run(max_files=int(session.settings["max_coverage_files"]))
    st["coverage"] = outcome
    if outcome.layout is not None:
        st["layout"] = outcome.layout
        st.pop("orchestrator", None)

    if not outcome.accepted:
        return

    # Coverage tests can fail like any others, and they are written *after*
    # stage 6 has run -- so without this pass they never reach the repair loop
    # at all. They would then sit failing in the final report and block the
    # mutation phase's green gate, making the run look worse than the pipeline
    # actually is. ``pipeline.py`` does the same thing on the CLI path.
    _collect_suite(session, config, baseline=False)
    if st.get("failures"):
        st["coverage_repaired"] = len(st["failures"])
        _repair_pass(session, config, merge=True)
        _collect_suite(session, config, baseline=False)


def _stage_mutation(session: Session, config) -> None:
    """Runs after repair because the phase refuses to score a failing suite."""
    from ..enhance import MutationPhase

    session.reset_from(8)
    st = session.state
    outcome = MutationPhase(
        st["layout"], st["environment"], config, _llm(session, config)
    ).run(
        max_mutants=int(session.settings["max_mutants"]),
        max_survivors=int(session.settings["max_survivors"]),
    )
    st["mutation"] = outcome
    if outcome.layout is not None:
        st["layout"] = outcome.layout
        st.pop("orchestrator", None)


def _stage_report(session: Session, config) -> None:
    from ..runner import TestRunner

    session.reset_from(9)
    st = session.state
    st["after"] = TestRunner(st["layout"], st["environment"], config).run_suite()


STAGE_FUNCTIONS = {
    1: _stage_ingest,
    2: _stage_environment,
    3: _stage_run_suite,
    4: _stage_generate,
    5: _stage_diagnose,
    6: _stage_repair,
    7: _stage_coverage,
    8: _stage_mutation,
    9: _stage_report,
}


def _blocked_reason(session: Session, stage: int) -> Optional[str]:
    """Why this stage cannot start yet, or None."""
    st = session.state
    if stage == 1:
        return None if session.source else "Choose a project first."
    if stage == 2:
        return None if "layout" in st else "Run stage 1 first."
    if stage == 3:
        return None if "environment" in st else "Run stage 2 first."
    if stage in (4, 7, 8, 9):
        return None if "before" in st else "Run stage 3 first."
    if stage == 5:
        return None if st.get("failures") else "Nothing is failing to diagnose."
    if stage == 6:
        records = st.get("records") or {}
        if records and all(r.diagnosis is not None for r in records.values()):
            return None
        return "Run stage 5 first."
    return None


def _run_stage(session: Session, stage: int) -> None:
    """Worker body. Owns ``session.running`` for its lifetime."""
    started = time.time()
    handler = LogCollector(session.logs)
    root = logging.getLogger("autef2")
    root.addHandler(handler)
    try:
        config = _config(session)
        STAGE_FUNCTIONS[stage](session, config)
        session.done[stage] = True
    except StageError as exc:
        session.error = str(exc)
    except Exception as exc:  # pragma: no cover - surfaced to the user
        session.error = f"{type(exc).__name__}: {exc}"
        logger.error("stage %s failed\n%s", stage, traceback.format_exc())
    finally:
        root.removeHandler(handler)
        session.elapsed += time.time() - started
        session.running = None


def _worker(session: Session) -> None:
    """Runs the queue until it empties or a stage fails."""
    while True:
        with session.lock:
            if session.error or not session.queue:
                session.queue = []
                session.running = None
                return
            stage = session.queue.pop(0)
            blocked = _blocked_reason(session, stage)
            # In a chained run, diagnosis and repair are skipped when nothing
            # is failing: a green project should still reach coverage,
            # mutation and the report.
            if blocked and stage in (5, 6):
                continue
            if blocked:
                session.error = blocked
                session.queue = []
                session.running = None
                return
            session.running = stage
        _run_stage(session, stage)


def _start(session: Session, stages: List[int]) -> Optional[str]:
    with session.lock:
        if session.running is not None or session.queue:
            return "A stage is already running."
        session.error = None
        session.queue = list(stages)
    threading.Thread(target=_worker, args=(session,), daemon=True).start()
    return None


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def _suite_summary(result) -> Dict[str, Any]:
    return {
        "ran": result.ran,
        "passed": len(result.passed),
        "failed": len(result.failures),
        "skipped": len(result.skipped),
        "collection_errors": len(result.collection_errors),
        "duration_s": round(result.duration_s, 1),
        "timed_out": getattr(result, "timed_out", False),
    }


def _written_files(records) -> List[Dict[str, Any]]:
    """The test files a phase wrote, for the on-demand viewer."""
    return [
        {
            "file": Path(r.test_file).name if r.test_file else "",
            "path": r.test_file or "",
            "module": r.module_import,
            "kept": r.accepted,
            "collected": r.tests_collected,
            "passing": r.tests_passing,
            "error": r.error or "",
        }
        for r in records
    ]


def _mutants(outcome) -> List[Dict[str, Any]]:
    """Every mutant, and what happened to it.

    Counts alone cannot be explained. "One survived" invites the question
    "which one, and why", and the answer -- the file, the line, the operator
    and the exact change -- is the difference between a reported number and a
    finding somebody can check.

    Killer tests are matched back to their mutant by file and line, so a
    survivor that was attempted and rejected carries the reason it was
    rejected rather than just failing to appear.
    """
    before = outcome.before
    if before is None or not before.measured:
        return []
    # ``after`` carries the post-generation state: same mutants, but any that a
    # written test went on to kill are marked there and not in ``before``.
    after = outcome.after or before

    # The phase writes one killer test per survivor, in order, so records pair
    # positionally with the survivors it attempted -- and only with those. The
    # attempt belongs to one mutant; attaching it to every mutant in the file
    # would report a rejection against mutants nothing was ever written for.
    attempted = [m for m in before.survivors() if not m.error]
    attempt_for = {
        (m.file, m.lineno, m.operator): record
        for m, record in zip(attempted, outcome.records)
    }

    out: List[Dict[str, Any]] = []
    for mutant in after.mutants:
        record = attempt_for.get((mutant.file, mutant.lineno, mutant.operator))
        out.append({
            "file": Path(mutant.file).name,
            "line": mutant.lineno,
            "operator": mutant.operator,
            "original": mutant.original,
            "mutated": mutant.mutated,
            "killed": mutant.killed,
            "killed_by": mutant.killed_by,
            # A mutant the suite could not judge at all: not a kill, and not a
            # gap in the tests either, so it is excluded from the score.
            "error": mutant.error,
            "killed_after_generation": mutant.killed_after_generation,
            # Whether a killer test was attempted for this mutant, and if it was
            # rejected, why -- the answer to "one survived, what happened".
            "attempted": record is not None,
            "attempt_error": (
                record.error if record is not None and not record.accepted else None
            ),
        })
    return out


def _snapshot(session: Session) -> Dict[str, Any]:
    from ..config import resolve_api_key

    st = session.state
    out: Dict[str, Any] = {
        "username": session.username,
        "source_label": session.source_label or (session.source or ""),
        "has_source": bool(session.source),
        "running": session.running,
        "running_name": STAGE_NAMES.get(session.running or 0),
        "queued": list(session.queue),
        "error": session.error,
        "elapsed_s": round(session.elapsed, 1),
        "settings": session.settings,
        "done": {str(k): v for k, v in session.done.items()},
        "billed_stages": sorted(BILLED_STAGES),
        "stage_names": {str(k): v for k, v in STAGE_NAMES.items()},
        "blocked": {str(n): _blocked_reason(session, n) for n in range(1, 10)},
        "logs": session.logs[-120:],
        "api_key_present": bool(resolve_api_key()),
    }

    layout = st.get("layout")
    if layout is not None:
        out["layout"] = {
            "name": layout.name,
            "style": layout.layout_style,
            "root": str(layout.root),
            "test_files": len(layout.test_files),
            "test_roots": [str(r) for r in layout.test_roots],
            "installable": layout.installable,
            "dependencies": len(layout.declared_dependencies),
            "notes": list(layout.notes),
            # A repository with no tests is not a repository whose tests all
            # pass. Reporting "nothing is failing" for one reads as success and
            # sends the reader looking for a bug that is not there.
            "no_tests": not layout.test_files,
        }

    env = st.get("environment")
    if env is not None:
        out["environment"] = {
            "isolated": env.isolated,
            "python": str(env.python),
            "installed": list(env.installed),
            "warnings": list(env.warnings),
        }

    if st.get("before") is not None:
        out["before"] = _suite_summary(st["before"])
    if st.get("after") is not None:
        out["after"] = _suite_summary(st["after"])

    out["failures"] = [
        {
            "nodeid": f.nodeid,
            "exception": f.exception_type,
            "message": (f.exception_message or "")[:220],
            "phase": f.phase,
        }
        for f in (st.get("failures") or [])
    ]

    records = st.get("records") or {}
    out["records"] = [
        {
            "nodeid": nodeid,
            "cause": r.diagnosis.root_cause.value if r.diagnosis else None,
            "summary": (getattr(r.diagnosis, "summary", "") or "") if r.diagnosis else "",
            "fixed": bool(r.fixed),
            "weakened": bool(r.weakened),
            "regression": bool(r.caused_regression),
            "skipped_reason": r.skipped_reason,
            "failing": (st.get("failing_source") or {}).get(nodeid),
            "attempts": [
                {
                    "n": a.attempt,
                    "strategy": a.strategy_id,
                    "label": a.strategy_label,
                    "verified": a.verified_pass,
                    "rejected": a.rejected_reason,
                    "patch": a.patch_preview or "",
                }
                for a in r.attempts
            ],
        }
        for nodeid, r in records.items()
    ]

    gen = st.get("generation")
    if gen is not None:
        out["generation"] = {
            "considered": gen.considered,
            "accepted": len(gen.accepted),
            "tests_added": gen.tests_added,
            "records": [
                {
                    "module": g.module_import,
                    "file": Path(g.test_file).name if g.test_file else "",
                    "path": g.test_file or "",
                    "kept": g.accepted,
                    "collected": g.tests_collected,
                    "passing": g.tests_passing,
                    "error": g.error or "",
                }
                for g in gen.records
            ],
        }

    cov = st.get("coverage")
    if cov is not None:
        before = cov.before
        after = cov.after or cov.before
        out["coverage"] = {
            "measured": bool(before and before.measured),
            "error": (before.error if before else None),
            "line_before": round((before.line_rate if before else 0) * 100, 1),
            "line_after": round((after.line_rate if after else 0) * 100, 1),
            "branch_before": round((before.branch_rate if before else 0) * 100, 1),
            "branch_after": round((after.branch_rate if after else 0) * 100, 1),
            "statements": before.statements if before else 0,
            "written": len(cov.accepted),
            "repaired": st.get("coverage_repaired", 0),
            "files": _written_files(cov.records),
        }

    mut = st.get("mutation")
    if mut is not None:
        before = mut.before
        after = mut.after or mut.before
        out["mutation"] = {
            "measured": bool(before and before.measured),
            "skipped_reason": mut.skipped_reason,
            "score_before": round((before.score if before else 0) * 100),
            "score_after": round((after.score if after else 0) * 100),
            "killed_before": before.killed if before else 0,
            "killed_after": after.killed if after else 0,
            "total": before.total if before else 0,
            "newly_killed": mut.newly_killed,
            "written": len(mut.accepted),
            "files": _written_files(mut.records),
            "mutants": _mutants(mut),
        }

    llm = st.get("llm")
    out["usage"] = (
        {
            "calls": llm.usage.calls,
            "prompt_tokens": llm.usage.prompt_tokens,
            "completion_tokens": llm.usage.completion_tokens,
            "cost_usd": round(llm.usage.cost_usd, 6),
        }
        if llm is not None
        else {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    )
    return out


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _label_for(source: str) -> str:
    """A bare project name from a URL or a path.

    Splitting on "/" alone is not enough: a Windows path contains none, so the
    whole path became the name. That name is used as a directory under the
    workspace, and ``projects_dir / "<absolute path>"`` collapses back onto the
    original -- which once had a run seeding faults into the caller's own
    directory instead of a working copy.
    """
    text = source.strip().rstrip("/\\")
    for suffix in (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".git"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
    tail = PurePath(text.replace("\\", "/")).name
    # A GitHub URL ending in /tree/<branch> names the branch, not the project.
    return "".join(c for c in tail if c.isalnum() or c in "-_.") or "project"


def _read_project_file(session: Session, wanted: str):
    """One file from the project under test, for the code viewer.

    Confined to the working copy. The path arrives from the browser, and a
    signed-in user must not be able to read the rest of the machine through a
    text box -- ``..`` is what this exists to stop.
    """
    layout = session.state.get("layout")
    if layout is None or not wanted:
        return {"error": "No project loaded."}, 400

    root = Path(layout.root).resolve()
    try:
        target = (root / wanted).resolve() if not Path(wanted).is_absolute() else Path(wanted).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return {"error": "That file is outside the project."}, 403

    if not target.is_file():
        return {"error": "No such file."}, 404
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": str(exc)}, 500

    return {
        "path": str(target.relative_to(root)),
        "content": text[:200_000],
        "lines": len(text.splitlines()),
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "AUTEF/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter than the default
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("upload too large")
        return self.rfile.read(length) if length else b""

    def _payload(self) -> Dict[str, Any]:
        try:
            raw = self._body()
        except ValueError:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _session(self) -> Optional[Session]:
        token = self.headers.get("X-Autef-Token") or ""
        if not token:
            return None
        with SESSIONS_LOCK:
            return SESSIONS.get(token)

    def _download(self, session: Session, route: str) -> None:
        from . import report

        raw = session.state["layout"].name or "project"
        name = "".join(c for c in PurePath(str(raw)).name if c.isalnum() or c in "-_.")
        name = name or "project"
        if route == "/api/project.zip":
            body = report.build_zip(session) or b""
            ctype, filename = "application/zip", f"autef2-{name}.zip"
        elif route == "/api/report.json":
            body = json.dumps(
                report.build(session), indent=2, default=str
            ).encode("utf-8")
            ctype, filename = "application/json", f"autef2-{name}.json"
        else:
            body = report.render_html(report.build(session)).encode("utf-8")
            ctype, filename = "text/html; charset=utf-8", f"autef2-{name}.html"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route, _, query = self.path.partition("?")

        if route == "/api/state":
            session = self._session()
            if session is None:
                self._json({"error": "not signed in"}, 401)
                return
            self._json(_snapshot(session))
            return

        if route in ("/api/report.html", "/api/report.json", "/api/project.zip"):
            session = self._session()
            if session is None:
                self._json({"error": "not signed in"}, 401)
                return
            if "layout" not in session.state:
                self._json({"error": "Run stage 1 first."}, 400)
                return
            self._download(session, route)
            return

        if route == "/api/file":
            session = self._session()
            if session is None:
                self._json({"error": "not signed in"}, 401)
                return
            self._json(*_read_project_file(session, parse_qs(query).get("path", [""])[0]))
            return

        self._static(route)

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]

        if route == "/api/login":
            data = self._payload()
            user = str(data.get("username", ""))
            password = str(data.get("password", ""))
            # Both halves compared, so a wrong username and a wrong password
            # take the same time.
            ok = hmac.compare_digest(user, DEFAULT_USERNAME) & hmac.compare_digest(
                password, DEFAULT_PASSWORD
            )
            if not ok:
                time.sleep(0.4)
                self._json({"error": "Incorrect username or password."}, 401)
                return
            token = secrets.token_urlsafe(24)
            with SESSIONS_LOCK:
                SESSIONS[token] = Session(user)
            self._json({"token": token, "username": user})
            return

        session = self._session()
        if session is None:
            self._json({"error": "not signed in"}, 401)
            return

        if route == "/api/logout":
            token = self.headers.get("X-Autef-Token") or ""
            with SESSIONS_LOCK:
                SESSIONS.pop(token, None)
            self._json({"ok": True})
            return

        if route == "/api/config":
            data = self._payload()
            for key in list(session.settings):
                if key in data:
                    session.settings[key] = data[key]
            self._json(_snapshot(session))
            return

        if route == "/api/source":
            value = str(self._payload().get("value", "")).strip()
            if not value:
                self._json({"error": "Give a URL or a path."}, 400)
                return
            session.source = value
            session.source_label = _label_for(value)
            session.reset_pipeline()
            self._json(_snapshot(session))
            return

        if route == "/api/upload":
            name = self.headers.get("X-Autef-Filename") or "project.zip"
            try:
                raw = self._body()
            except ValueError:
                self._json({"error": "That file is too large."}, 413)
                return
            if not raw:
                self._json({"error": "Empty upload."}, 400)
                return
            config = _config(session)
            uploads = Path(config.workspace) / "uploads"
            uploads.mkdir(parents=True, exist_ok=True)
            target = uploads / Path(name).name
            target.write_bytes(raw)
            session.source = str(target)
            session.source_label = Path(name).stem
            session.reset_pipeline()
            self._json(_snapshot(session))
            return

        if route == "/api/stage":
            stage = int(self._payload().get("stage") or 0)
            if stage not in STAGE_FUNCTIONS:
                self._json({"error": "No such stage."}, 400)
                return
            blocked = _blocked_reason(session, stage)
            if blocked:
                self._json({"error": blocked}, 400)
                return
            error = _start(session, [stage])
            if error:
                self._json({"error": error}, 409)
                return
            self._json(_snapshot(session))
            return

        if route == "/api/run-all":
            error = _start(session, list(range(1, 10)))
            if error:
                self._json({"error": error}, 409)
                return
            self._json(_snapshot(session))
            return

        if route == "/api/reset":
            if session.running is not None:
                self._json({"error": "A stage is running."}, 409)
                return
            session.reset_pipeline()
            session.state.pop("llm", None)
            self._json(_snapshot(session))
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    """Run until interrupted."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"AUTEF v2  ->  http://{host}:{port}")
    print(f"Sign in as  {DEFAULT_USERNAME} / {DEFAULT_PASSWORD}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
