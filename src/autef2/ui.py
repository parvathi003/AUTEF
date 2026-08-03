"""Streamlit front end.

    streamlit run src/autef2/ui.py

Three tabs:

* **Repair** drives the pipeline one stage at a time -- ingest, environment,
  run the suite, generate tests, diagnose, repair and verify, coverage,
  mutation, report -- so each stage's output can be inspected before the next
  one starts. "Run all stages" chains them. v1's generation, coverage and
  mutation phases are stages 4, 7 and 8 of that one sequence rather than a
  separate menu: generation feeds the repair loop the failures it works on,
  and coverage and mutation are measured over the repaired suite.
* **Compare** runs v1's repair and v2's over the *same* repository and reports
  the difference test by test, with the efficiency figures beside it.
* **Benchmark** runs both arms over a manifest of projects and reports the five
  comparison metrics across a sample.

The stages are presentation only. Each one calls the same function the CLI
calls, so the demo and the measurement path cannot drift apart: ingest ->
prepare_environment -> TestRunner.run_suite -> RepairOrchestrator.diagnose ->
RepairOrchestrator.repair. Where v1's UI drove a fixed six-button sequence over
one known application, this one accepts any archive, directory or GitHub link
and reports what it found -- including when a project is out of scope, which is
a legitimate outcome rather than an error.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import zipfile
from collections import deque
from pathlib import Path
from typing import Optional

# Allow `streamlit run src/autef2/ui.py` without installing the package.
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from autef2.config import AutefConfig, resolve_api_key  # noqa: E402
from autef2.guards import detect_regressions  # noqa: E402
from autef2.ingest import IngestError, ingest  # noqa: E402
from autef2.llm import LLMClient, LLMError  # noqa: E402
from autef2.models import NON_REPAIRABLE, RunReport  # noqa: E402
from autef2.orchestrator import RepairOrchestrator  # noqa: E402
from autef2.pipeline import summarise  # noqa: E402
from autef2.resolver import resolve_all  # noqa: E402
from autef2.runner import TestRunner  # noqa: E402
from autef2.venv_manager import prepare_environment  # noqa: E402

REPO_ROOT = _SRC.parent

st.set_page_config(page_title="AUTEF v2", layout="wide")


# ---------------------------------------------------------------------------
# live log capture
# ---------------------------------------------------------------------------

#: Module level, so it survives Streamlit's re-execution of this script.
_LOG_LINES: deque = deque(maxlen=400)


class _DequeHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_LINES.append(
                f"{record.levelname[:4]:4} {record.name.split('.')[-1]:14} "
                f"{record.getMessage()}"
            )
        except Exception:  # noqa: BLE001 - logging must never break the UI
            pass


def _install_log_capture() -> None:
    logger = logging.getLogger("autef2")
    if getattr(logger, "_autef_ui_handler", None) is None:
        handler = _DequeHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger._autef_ui_handler = handler  # type: ignore[attr-defined]


_install_log_capture()


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

STAGE_KEYS = (
    "layout", "environment", "before", "failures", "records", "after",
    "orchestrator", "llm", "baseline_passing", "stage_error", "elapsed_s",
    "bench", "comparison", "generation", "coverage", "mutation",
)


def _reset_from(stage: int) -> None:
    """Drop everything a later stage derived, so the display cannot lie."""
    order = {
        1: ("layout", "environment", "before", "failures", "records", "after",
            "orchestrator", "baseline_passing", "elapsed_s", "generation",
            "coverage", "mutation"),
        2: ("environment", "before", "failures", "records", "after",
            "orchestrator", "baseline_passing", "generation", "coverage",
            "mutation"),
        3: ("before", "failures", "records", "after", "baseline_passing",
            "generation", "coverage", "mutation"),
        # Generation writes test files, so everything measured over the test
        # set goes -- but not ``before``, which describes the project as it
        # arrived and is what the report compares against.
        4: ("generation", "failures", "records", "after", "baseline_passing",
            "coverage", "mutation"),
        5: ("records", "after"),
        6: ("after",),
        # Coverage adds test files; the mutation score was measured against the
        # suite without them.
        7: ("coverage", "mutation", "after"),
        8: ("mutation", "after"),
        9: (),
    }
    for key in order.get(stage, ()):  # pragma: no branch
        st.session_state.pop(key, None)
    st.session_state.pop("stage_error", None)


def _workspace() -> Path:
    """The one workspace this session works in.

    ``AutefConfig`` mints a fresh temp workspace by default. Streamlit re-runs
    this script on every click, so without pinning it, stage 2 would look for
    the project stage 1 unpacked into a directory that no longer applies.
    """
    if "workspace" not in st.session_state:
        st.session_state["workspace"] = str(AutefConfig().workspace)
    return Path(st.session_state["workspace"])


def _config() -> AutefConfig:
    """Build a config for the current sidebar settings."""
    return AutefConfig.from_env(
        workspace=_workspace(),
        model=st.session_state.get("model", "gpt-4o-mini"),
        max_attempts=st.session_state.get("max_attempts", 3),
        use_venv=st.session_state.get("use_venv", False),
        use_signature_cache=st.session_state.get("use_cache", True),
    )


def _llm(config: AutefConfig) -> Optional[LLMClient]:
    """One client per session, so token and cost tallies accumulate."""
    if "llm" not in st.session_state:
        try:
            st.session_state["llm"] = LLMClient(config)
        except LLMError as exc:
            st.session_state["stage_error"] = str(exc)
            return None
    return st.session_state["llm"]


def _orchestrator(config: AutefConfig) -> Optional[RepairOrchestrator]:
    llm = _llm(config)
    if llm is None:
        return None
    if "orchestrator" not in st.session_state:
        st.session_state["orchestrator"] = RepairOrchestrator(
            st.session_state["layout"],
            st.session_state["environment"],
            config,
            llm,
        )
    return st.session_state["orchestrator"]


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------


def _sidebar() -> None:
    with st.sidebar:
        st.header("Run settings")
        st.selectbox(
            "Model", ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"], key="model",
            help="Held constant across arms when measuring.",
        )
        st.slider(
            "Escalation rungs per test", 1, 5, 3, key="max_attempts",
            help="How many different repair strategies to try before giving up.",
        )
        st.checkbox(
            "Isolated virtualenv per project", value=False, key="use_venv",
            help="Installs the project's own dependencies. Correct but slow; "
                 "needed for any project with third-party imports.",
        )
        st.checkbox(
            "Reuse strategies for repeated failures", value=True, key="use_cache",
            help="Skips diagnosis when a failure has the same shape as one "
                 "already repaired.",
        )
        limit = st.number_input(
            "Limit to first N failing tests (0 = all)", 0, 500, 0, key="max_tests_raw"
        )
        st.session_state["max_tests"] = int(limit) or None

        st.divider()
        st.caption("Phase limits")
        st.number_input(
            "Modules to generate for", 1, 50, 5, key="max_modules",
            help="Generation writes one test file per module, most testable "
                 "surface first.",
        )
        st.number_input(
            "Files to write coverage tests for", 1, 30, 3, key="max_coverage_files"
        )
        st.number_input(
            "Mutants to score", 1, 200, 20, key="max_mutants",
            help="Each mutant is one suite run, so this is the main cost of the "
                 "mutation phase.",
        )
        st.number_input(
            "Surviving mutants to write tests for", 1, 30, 5, key="max_survivors"
        )

        st.divider()
        key = resolve_api_key()
        st.caption(f"API key: {'found' if key else 'not found'}")
        st.caption(f"Workspace: `{_workspace()}`")
        if st.button("Reset session", width="stretch"):
            for key_name in list(st.session_state.keys()):
                if key_name in STAGE_KEYS or key_name in ("source_label", "workspace"):
                    st.session_state.pop(key_name, None)
            _LOG_LINES.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def _stage_ingest(source, config: AutefConfig, name_hint: Optional[str]) -> bool:
    _reset_from(1)
    try:
        layout = ingest(source, config, name_hint=name_hint)
    except IngestError as exc:
        st.session_state["stage_error"] = str(exc)
        return False
    st.session_state["layout"] = layout
    return True


def _stage_environment(config: AutefConfig) -> bool:
    _reset_from(2)
    environment = prepare_environment(st.session_state["layout"], config)
    st.session_state["environment"] = environment
    return True


def _collect_suite(config: AutefConfig, *, baseline: bool) -> bool:
    """Run the suite and refresh the list of failures to work from.

    ``before`` is written on the baseline pass only. Stages 4 and 7 add test
    files and then call this again, and a later run must not rewrite the
    snapshot that describes the project as it arrived -- that snapshot is the
    "before" half of the report.
    """
    runner = TestRunner(
        st.session_state["layout"], st.session_state["environment"], config
    )
    result = runner.run_suite()
    if baseline:
        st.session_state["before"] = result

    if not result.ran:
        if result.timed_out:
            # The tail is sliced from the end, so the "[timed out]" marker at
            # its head never survives. Saying "could not be executed" under a
            # screen of passing dots is the one reading that is plainly wrong.
            reason = (
                f"The suite was still running after {config.suite_timeout_s}s "
                "and was stopped, so its result is incomplete. Usually one test "
                "is blocking -- waiting on input, a socket, or a subprocess. "
                "Raise the timeout, or narrow the project."
            )
        else:
            reason = (
                "The test suite could not be executed, so this project is out "
                "of scope for repair."
            )
        st.session_state["stage_error"] = reason + " " + result.stdout_tail[-400:]
        st.session_state["failures"] = []
        return False

    failures = resolve_all(
        list(result.failures) + list(result.collection_errors),
        st.session_state["layout"],
    )
    limit = st.session_state.get("max_tests")
    if limit:
        failures = failures[:limit]
    st.session_state["failures"] = failures
    st.session_state["baseline_passing"] = list(result.passed)
    return True


def _stage_run_suite(config: AutefConfig) -> bool:
    _reset_from(3)
    return _collect_suite(config, baseline=True)


def _stage_diagnose(config: AutefConfig) -> bool:
    _reset_from(5)
    orchestrator = _orchestrator(config)
    if orchestrator is None:
        return False

    failures = st.session_state.get("failures") or []
    records = {}
    progress = st.progress(0.0, text="Diagnosing...")
    for index, failure in enumerate(failures, start=1):
        progress.progress(
            index / max(len(failures), 1), text=f"Diagnosing {failure.nodeid}"
        )
        records[failure.nodeid] = orchestrator.diagnose(failure)
    progress.empty()

    st.session_state["records"] = records
    return True


def _stage_repair(config: AutefConfig) -> bool:
    _reset_from(6)
    orchestrator = _orchestrator(config)
    if orchestrator is None:
        return False

    failures = st.session_state.get("failures") or []
    records = st.session_state.get("records") or {}
    baseline_passing = st.session_state.get("baseline_passing") or []

    progress = st.progress(0.0, text="Repairing...")
    live = st.empty()
    for index, failure in enumerate(failures, start=1):
        progress.progress(
            index / max(len(failures), 1), text=f"Repairing {failure.nodeid}"
        )
        record = orchestrator.repair(
            failure, baseline_passing, record=records.get(failure.nodeid)
        )
        records[failure.nodeid] = record
        live.markdown(
            f"`{_status(record)}` **{failure.nodeid}** - "
            f"{record.attempts_used} attempt(s)"
        )
    progress.empty()
    live.empty()

    st.session_state["records"] = records
    return True


def _stage_generate(config: AutefConfig) -> bool:
    """Write tests for modules that have none. v1's Test Generation Agent.

    Placed after the baseline run and before diagnosis on purpose: a generated
    test that fails is exactly what the repair loop exists for, so generation
    feeds stages 5 and 6 rather than sitting beside them.
    """
    from autef2.enhance import GenerationPhase

    llm = _llm(config)
    if llm is None:
        return False

    # After the model is known to be reachable: a stage that cannot start must
    # not first throw away the failure list stage 3 produced.
    _reset_from(4)
    outcome = GenerationPhase(
        st.session_state["layout"], st.session_state["environment"], config, llm
    ).run(max_modules=st.session_state.get("max_modules", 5))
    st.session_state["generation"] = outcome

    if outcome.layout is not None:
        # New test files change the test roots, and every later stage reads them.
        st.session_state["layout"] = outcome.layout
    st.session_state.pop("orchestrator", None)
    # Re-collect so the failures stage 5 diagnoses include the generated ones.
    return _collect_suite(config, baseline=False)


def _stage_coverage(config: AutefConfig) -> bool:
    """Measure coverage and write tests for the gaps. v1's Coverage Agent."""
    from autef2.enhance import CoveragePhase

    llm = _llm(config)
    if llm is None:
        return False

    _reset_from(7)
    outcome = CoveragePhase(
        st.session_state["layout"], st.session_state["environment"], config, llm
    ).run(max_files=st.session_state.get("max_coverage_files", 3))
    st.session_state["coverage"] = outcome

    if outcome.layout is not None:
        st.session_state["layout"] = outcome.layout
        st.session_state.pop("orchestrator", None)
    if outcome.accepted:
        # Coverage tests can fail like any others. Surfacing them here is what
        # lets stages 5 and 6 be re-run over them; leaving them hidden would
        # make the run look worse than the pipeline actually is.
        return _collect_suite(config, baseline=False)
    return True


def _stage_mutation(config: AutefConfig) -> bool:
    """Score against mutated source and write killer tests. v1's Mutation Agent.

    Runs after repair because the phase refuses to score a suite that is not
    green: against a suite that already fails, every mutant looks killed.
    """
    from autef2.enhance import MutationPhase

    llm = _llm(config)
    if llm is None:
        return False

    _reset_from(8)
    outcome = MutationPhase(
        st.session_state["layout"], st.session_state["environment"], config, llm
    ).run(
        max_mutants=st.session_state.get("max_mutants", 20),
        max_survivors=st.session_state.get("max_survivors", 5),
    )
    st.session_state["mutation"] = outcome

    if outcome.layout is not None:
        st.session_state["layout"] = outcome.layout
        st.session_state.pop("orchestrator", None)
    return True


def _stage_report(config: AutefConfig) -> bool:
    _reset_from(9)
    runner = TestRunner(
        st.session_state["layout"], st.session_state["environment"], config
    )
    st.session_state["after"] = runner.run_suite()
    return True


def _timed(step) -> bool:
    """Run a stage, adding its wall clock to the session total.

    The stages run across separate Streamlit executions, so the elapsed time
    has to be accumulated rather than measured from one start to one finish.
    """
    started = time.monotonic()
    try:
        return step()
    finally:
        st.session_state["elapsed_s"] = (
            st.session_state.get("elapsed_s", 0.0) + time.monotonic() - started
        )


def _run_all(source, config: AutefConfig, name_hint: Optional[str]) -> None:
    """Chain all nine stages.

    Diagnosis and repair are skipped when nothing is failing -- a green project
    still gets the coverage and mutation phases, which is the whole point of
    them being stages rather than a side menu.
    """
    has_failures = lambda: bool(st.session_state.get("failures"))  # noqa: E731
    steps = [
        ("Ingesting the project", lambda: _stage_ingest(source, config, name_hint), None),
        ("Preparing the environment", lambda: _stage_environment(config), None),
        ("Running the test suite", lambda: _stage_run_suite(config), None),
        ("Generating tests", lambda: _stage_generate(config), None),
        ("Diagnosing failures", lambda: _stage_diagnose(config), has_failures),
        ("Repairing and verifying", lambda: _stage_repair(config), has_failures),
        ("Raising coverage", lambda: _stage_coverage(config), None),
        ("Scoring mutation", lambda: _stage_mutation(config), None),
        ("Re-running the suite", lambda: _stage_report(config), None),
    ]
    for label, step, required in steps:
        if required is not None and not required():
            continue
        with st.spinner(label + "..."):
            if not _timed(step):
                return


# ---------------------------------------------------------------------------
# repair tab
# ---------------------------------------------------------------------------


def _source_picker(prefix: str, *, allow_upload: bool = True):
    """The zip / URL / path chooser. Shared so both tabs accept the same inputs."""
    options = ["GitHub or archive URL", "Path on this machine"]
    if allow_upload:
        options.insert(0, "Upload .zip")

    mode = st.radio(
        "Source", options, horizontal=True, label_visibility="collapsed",
        key=f"{prefix}_mode",
    )

    if mode == "Upload .zip":
        uploaded = st.file_uploader(
            "Project archive (.zip)", type=["zip"], key=f"{prefix}_upload"
        )
        if uploaded is not None:
            return uploaded, Path(uploaded.name).stem
        return None, None

    if mode == "GitHub or archive URL":
        url = st.text_input(
            "Repository or archive URL",
            placeholder="https://github.com/owner/repo  (add /tree/<branch> for a branch)",
            key=f"{prefix}_url",
        )
        st.caption(
            "The tip of one branch is downloaded as an archive; no git clone "
            "and no full history."
        )
        return (url.strip() or None), None

    path = st.text_input(
        "Directory or archive path", placeholder=r"C:\path\to\project",
        key=f"{prefix}_path",
    )
    return (path.strip() or None), None


def _repair_tab() -> None:
    config = _config()

    st.subheader("Project")
    source, name_hint = _source_picker("repair")

    if source is None:
        st.info("Choose a project to begin. Nothing runs until you click a stage.")
        _render_log()
        return

    st.session_state["source_label"] = str(name_hint or source)

    st.subheader("Stages")
    st.caption(
        "One sequence: the project is ingested, its suite is measured as it "
        "arrived, tests are written, what fails is diagnosed and repaired, and "
        "coverage and mutation are raised over the repaired suite. Each stage "
        "uses the state the previous one produced, and re-running a stage "
        "discards everything derived from it."
    )

    have_layout = "layout" in st.session_state
    have_env = "environment" in st.session_state
    have_before = "before" in st.session_state
    have_failures = bool(st.session_state.get("failures"))
    have_records = bool(st.session_state.get("records"))
    diagnosed = have_records and all(
        r.diagnosis is not None for r in st.session_state["records"].values()
    )
    repaired = have_records and any(
        r.attempts for r in st.session_state["records"].values()
    )

    clicked = None
    stages = [
        ("1. Ingest", True),
        ("2. Environment", have_layout),
        ("3. Run suite", have_env),
        ("4. Generate tests", have_before),
        ("5. Diagnose", have_failures),
        ("6. Repair & verify", diagnosed),
        ("7. Coverage", have_before),
        ("8. Mutation", have_before),
        ("9. Report", repaired or have_before),
    ]
    # Two rows, both laid out on five columns so the buttons line up. One
    # sequence, wrapped -- not two groups.
    for row in (stages[:5], stages[5:]):
        for column, (label, enabled) in zip(st.columns(5), row):
            if column.button(label, disabled=not enabled, width="stretch"):
                clicked = label

    run_all = st.button(
        "Run all stages", type="primary", width="stretch",
        help="Chains stages 1 to 9. Stages 4 to 8 call the model. Diagnosis "
             "and repair are skipped if nothing is failing.",
    )

    if clicked or run_all:
        needs_key = run_all or clicked.startswith(("4.", "5.", "6.", "7.", "8."))
        if needs_key and not config.api_key:
            st.error(
                "No OpenAI API key found. Set OPENAI_API_KEY in the "
                "environment or in a .env file, or put a real key in "
                "OAI_CONFIG_LIST.json. Stages 1 to 3 and 9 work without one."
            )
        else:
            if run_all:
                _run_all(source, config, name_hint)
            else:
                stage_fns = {
                    "1.": lambda: _stage_ingest(source, config, name_hint),
                    "2.": lambda: _stage_environment(config),
                    "3.": lambda: _stage_run_suite(config),
                    "4.": lambda: _stage_generate(config),
                    "5.": lambda: _stage_diagnose(config),
                    "6.": lambda: _stage_repair(config),
                    "7.": lambda: _stage_coverage(config),
                    "8.": lambda: _stage_mutation(config),
                    "9.": lambda: _stage_report(config),
                }
                with st.spinner(f"Stage {clicked}..."):
                    _timed(stage_fns[clicked[:2]])
            # The button row was drawn before this stage ran, so the next
            # stage's button is still disabled in the page the user is looking
            # at. Re-run so enablement matches the state we just produced.
            st.rerun()

    if st.session_state.get("stage_error"):
        st.error(st.session_state["stage_error"])

    _render_layout()
    _render_environment()
    _render_suite()
    _render_generation()
    _render_diagnoses()
    _render_repairs()
    _render_coverage()
    _render_mutation()
    _render_report(config)
    _render_log()


def _render_layout() -> None:
    layout = st.session_state.get("layout")
    if layout is None:
        return
    with st.expander(
        f"Stage 1 - detected layout: **{layout.name}** ({layout.layout_style})",
        expanded=False,
    ):
        st.json(
            {
                "name": layout.name,
                "root": layout.root,
                "style": layout.layout_style,
                "installable": layout.installable,
                "import_roots": layout.import_roots,
                "test_roots": layout.test_roots,
                "source_roots": layout.source_roots,
                "test_files": len(layout.test_files),
                "declared_dependencies": layout.declared_dependencies,
                "notes": layout.notes,
            }
        )
        st.caption(
            "Nothing here is hardcoded: the name comes from the project's own "
            "packaging metadata and the roots from its tree."
        )


def _render_environment() -> None:
    environment = st.session_state.get("environment")
    if environment is None:
        return
    with st.expander("Stage 2 - environment", expanded=False):
        st.code(environment.python, language="text")
        if getattr(environment, "warnings", None):
            for warning in environment.warnings:
                st.warning(warning)
        else:
            st.caption("No warnings.")


def _render_suite() -> None:
    before = st.session_state.get("before")
    if before is None:
        return

    st.subheader("Stage 3 - suite before repair")
    a, b, c, d = st.columns(4)
    a.metric("Passing", len(before.passed))
    b.metric("Failing", len(before.failures))
    c.metric("Collection errors", len(before.collection_errors))
    d.metric("Skipped", len(before.skipped))

    failures = st.session_state.get("failures") or []
    if not failures:
        if not before.ran:
            return
        layout = st.session_state.get("layout")
        if layout is not None and not layout.test_files:
            # "Nothing is failing" reads as success and is the wrong thing to
            # say about a project that has no tests at all. The two cases need
            # different next steps, so they get different messages.
            st.warning(
                "**This project has no test files.** Nothing was found matching "
                "`test_*.py`, `*_test.py`, `tests.py` or `test.py`, so there is "
                "nothing to repair.\n\n"
                "AUTEF v2 diagnoses and repairs *existing* failing tests; it "
                "does not write new ones. To exercise the repair loop, choose a "
                "project that ships a test suite, or seed faults into one with "
                "the Compare tab."
            )
        elif before.total == 0:
            st.warning(
                "Test files were found but pytest collected no tests from them. "
                "The suite may use a runner other than pytest."
            )
        else:
            st.success(
                f"All {len(before.passed)} tests pass, so there is nothing to "
                "repair. Seed faults from the Compare tab to exercise the loop."
            )
        return

    rows = [
        {
            "test": f.nodeid,
            "exception": f.exception_type,
            "message": (f.exception_message or "")[:90],
            "phase": f.phase,
            "test file resolved": Path(f.test_file).name if f.test_file else "-",
            "source resolved": ", ".join(Path(s).name for s in f.source_files) or "-",
        }
        for f in failures
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(
        "File locations come from the pytest nodeid and the traceback frames, "
        "not from guessing filenames out of test class names."
    )


def _render_diagnoses() -> None:
    records = st.session_state.get("records") or {}
    if not records:
        return
    if not any(r.diagnosis for r in records.values()):
        return

    st.subheader("Stage 5 - diagnosis")

    unanswered = [
        r for r in records.values()
        if r.diagnosis is not None and not r.diagnosis.model_answered
    ]
    if unanswered:
        # These look like ordinary diagnoses -- a plausible root cause and a
        # confidence -- but no agent produced them. Saying so is the difference
        # between a degraded run and a working one.
        st.error(
            f"The model was not reached for {len(unanswered)} of "
            f"{len(records)} failure(s), so those root causes come from the "
            "static classifier alone, not from the Failure Analysis Agent. "
            "Repair will fail for the same reason.\n\n"
            + (unanswered[0].diagnosis.explanation or "")
        )

    rows = []
    for nodeid, record in records.items():
        diagnosis = record.diagnosis
        rows.append(
            {
                "test": nodeid,
                "root cause": diagnosis.root_cause.value if diagnosis else "",
                "at fault": diagnosis.at_fault if diagnosis else "",
                "confidence": round(diagnosis.confidence, 2) if diagnosis else None,
                "diagnosed by": (
                    ""
                    if diagnosis is None
                    else "model" if diagnosis.model_answered else "static classifier"
                ),
                "from cache": record.cache_hit,
                "repairable": bool(
                    diagnosis and diagnosis.root_cause not in NON_REPAIRABLE
                ),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    non_repairable = [
        (nodeid, r.diagnosis)
        for nodeid, r in records.items()
        if r.diagnosis and r.diagnosis.root_cause in NON_REPAIRABLE
    ]
    if non_repairable:
        st.info(
            f"{len(non_repairable)} failure(s) were diagnosed as not repairable "
            "by editing the test -- a production defect or a missing service. "
            "Editing the test there would hide the problem."
        )


def _render_repairs() -> None:
    records = st.session_state.get("records") or {}
    attempted = {k: v for k, v in records.items() if v.attempts or v.skipped_reason}
    if not attempted:
        return

    st.subheader("Stage 6 - repairs")

    spent = any(
        attempt.prompt_tokens or attempt.completion_tokens
        for record in attempted.values()
        for attempt in record.attempts
    )
    reason = next(
        (
            attempt.rejected_reason
            for record in attempted.values()
            for attempt in record.attempts
            if attempt.rejected_reason and "model call failed" in attempt.rejected_reason
        ),
        None,
    )
    if not spent and reason:
        st.error(
            "No model call succeeded, so nothing was actually repaired. "
            "The attempts below record the failure, not a judgement about the "
            f"tests.\n\n{reason}"
        )

    rows = [
        {
            "test": nodeid,
            "outcome": _status(record),
            "root cause": record.diagnosis.root_cause.value if record.diagnosis else "",
            "attempts": record.attempts_used,
            "strategies": " -> ".join(a.strategy_id for a in record.attempts),
            "weakened": record.weakened,
            "regression": record.caused_regression,
            "cost": round(record.cost_usd, 5),
        }
        for nodeid, record in attempted.items()
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    for nodeid, record in attempted.items():
        with st.expander(f"[{_status(record)}] {nodeid}"):
            if record.diagnosis:
                st.markdown(
                    f"**Diagnosis:** `{record.diagnosis.root_cause.value}` "
                    f"(confidence {record.diagnosis.confidence:.2f}, "
                    f"at fault: {record.diagnosis.at_fault})"
                )
                if record.diagnosis.explanation:
                    st.write(record.diagnosis.explanation)
                if not record.diagnosis.model_answered:
                    st.caption(
                        "The model was not reached; this label is the static "
                        "classifier's."
                    )
                elif (
                    record.diagnosis.heuristic_cause
                    and not record.diagnosis.llm_agreed
                ):
                    st.caption(
                        "The static classifier suggested "
                        f"`{record.diagnosis.heuristic_cause.value}`; the model "
                        "disagreed."
                    )
            if record.skipped_reason:
                st.warning(record.skipped_reason)

            for attempt in record.attempts:
                verdict = (
                    "verified pass"
                    if attempt.verified_pass
                    else attempt.rejected_reason
                    or attempt.new_failure
                    or "no effect"
                )
                st.markdown(
                    f"**Attempt {attempt.attempt}** - `{attempt.strategy_id}` "
                    f"({attempt.strategy_label}): {verdict}"
                )
                if attempt.weakening and attempt.weakening.reasons:
                    st.caption("Weakening: " + "; ".join(attempt.weakening.reasons))
                if attempt.patch_preview:
                    st.code(attempt.patch_preview, language="python")


def _render_generation() -> None:
    outcome = st.session_state.get("generation")
    if outcome is None:
        return

    st.subheader("Stage 4 - tests written for modules that had none")
    if not outcome.records:
        st.info(
            f"Nothing to generate for: {outcome.considered} module(s) considered, "
            "none with public functions or classes."
        )
        return

    a, b, c = st.columns(3)
    a.metric("Files kept", f"{len(outcome.accepted)}/{len(outcome.records)}")
    b.metric("Tests added", outcome.tests_added)
    c.metric(
        "Passing on arrival",
        sum(r.tests_passing for r in outcome.accepted),
        help="The rest are input to the repair loop, stages 5 and 6.",
    )

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "module": r.module_import,
                    "test file": Path(r.test_file).name if r.test_file else "-",
                    "kept": r.accepted,
                    "collected": r.tests_collected,
                    "passing": r.tests_passing,
                    "covers": ", ".join(r.units),
                    "problem": r.error or "",
                }
                for r in outcome.records
            ]
        ),
        width="stretch", hide_index=True,
    )
    st.caption(
        "A generated file is kept only if pytest can run it. Generated tests "
        "that *fail* are kept on purpose: they are what the repair loop is for."
    )


def _render_coverage() -> None:
    outcome = st.session_state.get("coverage")
    if outcome is None:
        return

    st.subheader("Stage 7 - coverage")
    before, after = outcome.before, outcome.after
    if before is None or not before.measured:
        st.warning(
            "Coverage could not be measured"
            + (f": {before.error}" if before and before.error else "")
        )
        return

    a, b, c, d = st.columns(4)
    a.metric(
        "Line coverage", f"{(after or before).line_rate:.0%}",
        delta=f"{outcome.line_gain * 100:+.0f} pts" if outcome.after else None,
    )
    b.metric(
        "Branch coverage", f"{(after or before).branch_rate:.0%}",
        delta=f"{outcome.branch_gain * 100:+.0f} pts" if outcome.after else None,
    )
    c.metric("Statements", f"{before.covered_statements}/{before.statements}")
    d.metric("Files given tests", len(outcome.accepted))

    if outcome.records:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "module": r.module_import,
                        "test file": Path(r.test_file).name if r.test_file else "-",
                        "kept": r.accepted,
                        "collected": r.tests_collected,
                        "passing": r.tests_passing,
                        "targeted": ", ".join(r.units),
                        "problem": r.error or "",
                    }
                    for r in outcome.records
                ]
            ),
            width="stretch", hide_index=True,
        )

    from autef2.coverage_tool import gaps

    remaining = gaps(after or before, limit=10)
    if remaining:
        with st.expander(f"Still uncovered ({len(remaining)} file(s))"):
            for file_coverage in remaining:
                st.markdown(
                    f"`{Path(file_coverage.path).name}` — "
                    f"{file_coverage.line_rate:.0%} lines, "
                    f"{file_coverage.branch_rate:.0%} branches; "
                    f"missing lines {file_coverage.missing_lines[:20]}"
                )


def _render_mutation() -> None:
    outcome = st.session_state.get("mutation")
    if outcome is None:
        return

    st.subheader("Stage 8 - mutation")
    if outcome.skipped_reason:
        st.warning(outcome.skipped_reason)
        return

    before, after = outcome.before, outcome.after
    if before is None or not before.measured:
        st.warning(
            "Mutation score could not be measured"
            + (f": {before.error}" if before and before.error else "")
        )
        return

    a, b, c, d = st.columns(4)
    a.metric(
        "Mutation score", f"{(after or before).score:.0%}",
        delta=f"{outcome.score_gain * 100:+.0f} pts" if outcome.after else None,
    )
    b.metric("Mutants killed", f"{(after or before).killed}/{before.total}")
    c.metric("Surviving", (after or before).survived)
    d.metric(
        "Verified killer tests", outcome.newly_killed,
        help="Tests that provably fail on the mutant and pass without it. Tests "
             "that did not manage both were discarded.",
    )

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "file": Path(m.file).name,
                    "line": m.lineno,
                    "mutation": m.operator,
                    "killed": m.killed,
                    "by a new test": m.killed_after_generation,
                    "killed by": m.killed_by or "",
                    "before": m.original,
                    "after": m.mutated,
                }
                for m in (after or before).mutants
            ]
        ),
        width="stretch", hide_index=True,
    )
    if outcome.records:
        rejected = [r for r in outcome.records if not r.accepted]
        if rejected:
            with st.expander(f"Rejected killer tests ({len(rejected)})"):
                for record in rejected:
                    st.markdown(f"- {record.units[0] if record.units else ''}: {record.error}")
    st.caption(
        "A mutation score only means something against a suite that passes, so "
        "this phase refuses to run while tests are failing."
    )


def _render_report(config: AutefConfig) -> None:
    before = st.session_state.get("before")
    after = st.session_state.get("after")
    records = st.session_state.get("records") or {}
    if before is None or after is None:
        return

    st.subheader("Stage 9 - result")
    fixed = [r for r in records.values() if r.fixed]
    weakened = [r for r in records.values() if r.weakened]
    regressions = detect_regressions(before.passed, after)

    a, b, c, d, e = st.columns(5)
    a.metric(
        "Passing", len(after.passed),
        delta=len(after.passed) - len(before.passed),
    )
    b.metric("Failing", len(after.failures),
             delta=len(after.failures) - len(before.failures))
    c.metric("Repaired", f"{len(fixed)}/{len(records)}")
    d.metric("Weakened", len(weakened))
    e.metric("Regressions", len(regressions))

    if regressions:
        st.error(
            "These tests passed before the repairs and do not now: "
            + ", ".join(regressions[:10])
        )
    if weakened:
        st.warning(
            f"{len(weakened)} accepted repair(s) weakened the assertion. This "
            "is the number a fix rate on its own would hide."
        )

    report = _as_run_report()
    st.code(summarise(report), language="text")

    left, right = st.columns(2)
    left.download_button(
        "Download report (JSON)",
        data=json.dumps(report.to_dict(), indent=2, default=str),
        file_name=f"autef2_{report.project}.json",
        mime="application/json",
        width="stretch",
    )
    archive = _zip_project()
    if archive is not None:
        right.download_button(
            "Download repaired project (.zip)",
            data=archive,
            file_name=f"{report.project}_repaired.zip",
            mime="application/zip",
            width="stretch",
        )


def _as_run_report() -> RunReport:
    """Assemble the same RunReport the CLI would have produced."""
    layout = st.session_state["layout"]
    report = RunReport(project=layout.name, layout=layout, arm="autef2")
    report.before = st.session_state.get("before")
    report.after = st.session_state.get("after")
    report.records = list((st.session_state.get("records") or {}).values())
    report.duration_s = st.session_state.get("elapsed_s", 0.0)
    llm = st.session_state.get("llm")
    if llm is not None:
        report.prompt_tokens = llm.usage.prompt_tokens
        report.completion_tokens = llm.usage.completion_tokens
        # Without this the report claims zero model calls however much it
        # spent, and ``model_calls_succeeded`` has to infer from token counts.
        report.llm_calls = llm.usage.calls
        report.cost_usd = llm.usage.cost_usd
    return report


def _zip_project() -> Optional[bytes]:
    """Zip the working copy so the repaired tests can be taken away."""
    layout = st.session_state.get("layout")
    if layout is None:
        return None
    root = Path(layout.root)
    files = [p for p in root.rglob("*") if p.is_file()]
    if sum(p.stat().st_size for p in files) > 64 * 1024 * 1024:
        st.caption("Project is too large to offer as a download.")
        return None

    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if any(
                part in {"__pycache__", ".pytest_cache", ".git"}
                for part in path.parts
            ):
                continue
            archive.write(path, path.relative_to(root))
    return buffer.getvalue()


def _status(record) -> str:
    if record.fixed:
        return "fixed"
    if record.skipped_reason:
        return "skipped"
    if record.attempts:
        return "not fixed"
    return "diagnosed"


def _render_log() -> None:
    if not _LOG_LINES:
        return
    with st.expander("Run log", expanded=False):
        st.code("\n".join(_LOG_LINES), language="text")


# ---------------------------------------------------------------------------
# compare tab: one repository, v1 versus v2
# ---------------------------------------------------------------------------


def _compare_tab() -> None:
    from autef2.eval.compare import compare_project, render_comparison

    config = _config()

    st.subheader("The same repository, both versions")
    st.caption(
        "v1's repair -- one generic prompt, one attempt, no verification -- and "
        "v2's diagnosed loop are run over the same project, from the same "
        "pristine copy, in the same virtualenv, on the same pytest and patching "
        "stack. Only the repair step differs, which is what makes the "
        "difference attributable to it."
    )

    source, _name = _source_picker("compare")

    left, middle, right = st.columns(3)
    inject = left.number_input(
        "Faults to seed", 0, 40, 8, key="cmp_inject",
        help="Real repositories mostly pass. Without seeding there is usually "
             "nothing for either version to repair, and nothing to compare. "
             "Faults go only into tests that currently pass.",
    )
    max_tests = middle.number_input(
        "Cap failing tests", 0, 100, 8, key="cmp_max",
        help="Bounds the cost of one comparison.",
    )
    seed = right.number_input("Seed", 0, 10_000, 1337, key="cmp_seed")

    from autef2.eval.faults import ALL_KINDS

    fault_kinds = st.multiselect(
        "Fault kinds to seed", list(ALL_KINDS), default=list(ALL_KINDS),
        key="cmp_kinds",
        help="The mix decides the result. v1 replaces a failing test "
             "*function*, so an import or setup fault -- which takes out the "
             "whole file -- is one it cannot attempt at all. A run heavy in "
             "those measures reach, not repair quality.",
    )

    if not config.api_key:
        st.warning("No API key found; neither version can call the model.")

    disabled = source is None or not config.api_key
    if st.button(
        "Run comparison", type="primary", disabled=disabled, key="cmp_run"
    ):
        with st.spinner("Running v1, then v2, on identical copies..."):
            try:
                result = compare_project(
                    source if isinstance(source, str) else source,
                    config,
                    inject=int(inject),
                    max_tests=int(max_tests) or None,
                    seed=int(seed),
                    output_dir=Path(config.workspace) / "compare",
                    fault_kinds=tuple(fault_kinds),
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                st.error(f"The comparison could not complete: {exc}")
                result = None
        if result is not None:
            st.session_state["comparison"] = result

    result = st.session_state.get("comparison")
    if result is None:
        if source is None:
            st.info("Choose a repository to compare.")
        _render_log()
        return

    if result.error:
        st.error(result.error)

    if getattr(result, "model_unavailable", False):
        # Showing metric tiles here would present "0 fixed, same" as a finding.
        st.info(
            "Nothing below would mean anything, so it is not shown. Restore the "
            "API key's quota and run the comparison again."
        )
        with st.expander("What the run did do"):
            st.write(
                f"Both versions were offered the same "
                f"{result.baseline.observations if result.baseline else 0} failing "
                "test(s), ingested the project, built the environment and ran the "
                "suite. Only the model calls failed."
            )
        _render_log()
        return

    baseline, candidate = result.baseline, result.candidate
    if baseline is None or candidate is None:
        _render_log()
        return

    st.subheader("How much better")
    a, b, c, d = st.columns(4)
    a.metric(
        "Tests fixed", f"{candidate.fixed} / {candidate.observations}",
        delta=candidate.fixed - baseline.fixed,
        help=f"v1 fixed {baseline.fixed} of the same {baseline.observations}.",
    )
    b.metric(
        "Fix rate (attempted)", f"{candidate.fix_rate_attempted:.0%}",
        delta=f"{(candidate.fix_rate_attempted - baseline.fix_rate_attempted) * 100:.0f} pts",
        help="Over the failures each version actually put to the model.",
    )
    c.metric(
        "Weakened fixes", candidate.weakened,
        delta=candidate.weakened - baseline.weakened,
        delta_color="inverse",
        help="Fixes that passed by gutting the assertion. Lower is better.",
    )
    d.metric(
        "Regressions", candidate.regressions_introduced,
        delta=candidate.regressions_introduced - baseline.regressions_introduced,
        delta_color="inverse",
    )

    st.subheader("How efficient")
    e, f, g, h = st.columns(4)
    e.metric(
        "Cost per fix",
        f"${candidate.cost_per_fix:.4f}" if candidate.fixed else "n/a",
        delta=(
            f"${candidate.cost_per_fix - baseline.cost_per_fix:+.4f}"
            if candidate.fixed and baseline.fixed
            else None
        ),
        delta_color="inverse",
        help="The figure that matters: a version that spends nothing and "
             "repairs nothing is cheap, not efficient.",
    )
    f.metric("Total cost", f"${candidate.cost_usd:.4f}",
             delta=f"${candidate.cost_usd - baseline.cost_usd:+.4f}",
             delta_color="off")
    g.metric("Model calls", candidate.llm_calls,
             delta=candidate.llm_calls - baseline.llm_calls, delta_color="off")
    h.metric(
        "Calls per fix",
        f"{candidate.calls_per_fix:.1f}" if candidate.fixed else "n/a",
        delta=(
            f"{candidate.calls_per_fix - baseline.calls_per_fix:+.1f}"
            if candidate.fixed and baseline.fixed
            else None
        ),
        delta_color="inverse",
    )

    st.subheader("Test by test")
    st.dataframe(
        pd.DataFrame([t.to_dict() for t in result.tests]),
        width="stretch", hide_index=True,
    )

    with st.expander("Full report", expanded=True):
        st.markdown(render_comparison(result))

    st.download_button(
        "Download comparison (JSON)",
        data=json.dumps(result.to_dict(), indent=2, default=str),
        file_name=f"compare_{result.project}.json",
        mime="application/json",
        width="stretch",
    )
    _render_log()


# ---------------------------------------------------------------------------
# benchmark tab
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST = {
    "projects": [
        {
            "name": "calcpkg",
            "source": "tests_autef2/sample_project",
            "stratum": "tiny",
            "inject": 4,
            "max_tests": 10,
        }
    ]
}


def _benchmark_tab() -> None:
    from autef2.eval.benchmark import ProjectSpec, run_benchmark, stratified_sample
    from autef2.eval.metrics import render_markdown

    config = _config()

    st.subheader("Baseline vs AUTEF v2")
    st.caption(
        "Both arms run on the same execution stack (pytest, traceback "
        "resolution, AST patching) over the same projects, from the same "
        "pristine copy. Only the repair step differs: one generic prompt "
        "against diagnose -> strategy -> verify -> escalate."
    )

    example = REPO_ROOT / "benchmarks" / "example_manifest.json"
    default_text = (
        example.read_text(encoding="utf-8")
        if example.is_file()
        else json.dumps(DEFAULT_MANIFEST, indent=2)
    )

    manifest_text = st.text_area(
        "Manifest (JSON)", value=default_text, height=260,
        help="Each project needs a source (directory, .zip, or GitHub URL), a "
             "stratum label, and optionally how many faults to seed.",
    )

    left, middle, right = st.columns(3)
    arms = left.multiselect("Arms", ["baseline", "autef2"], default=["baseline", "autef2"])
    per_stratum = middle.number_input("Projects per stratum (0 = all)", 0, 50, 0)
    seed = right.number_input("Seed", 0, 10_000, 1337)
    bench_cache = st.checkbox(
        "Leave the signature cache on", value=False,
        help="Off by default: with it on, a project's result depends on which "
             "projects ran before it.",
    )

    if not config.api_key:
        st.warning("No API key found; the benchmark cannot call the model.")

    if st.button("Run benchmark", type="primary", disabled=not arms):
        try:
            data = json.loads(manifest_text)
        except json.JSONDecodeError as exc:
            st.error(f"Manifest is not valid JSON: {exc}")
            return

        entries = data.get("projects", data) if isinstance(data, dict) else data
        try:
            specs = [ProjectSpec.from_dict(entry) for entry in entries]
        except (KeyError, TypeError) as exc:
            st.error(f"Manifest entry is missing a field: {exc}")
            return

        specs = stratified_sample(specs, int(per_stratum) or None, seed=int(seed))
        specs = [_resolve_spec_source(spec) for spec in specs]

        output_dir = Path(config.workspace) / "bench"
        with st.spinner(f"Running {len(specs)} project(s) x {len(arms)} arm(s)..."):
            result = run_benchmark(
                specs,
                config,
                arms=arms,
                seed=int(seed),
                output_dir=output_dir,
                use_cache=bench_cache,
            )
        st.session_state["bench"] = result

    result = st.session_state.get("bench")
    if result is None:
        _render_log()
        return

    if result.skipped:
        for entry in result.skipped:
            st.warning(f"{entry['project']}: {entry['reason']}")

    rows = []
    for arm, metrics in result.metrics_by_arm.items():
        rows.append(
            {
                "arm": arm,
                "projects processed": f"{metrics.projects_executed}/{metrics.projects}",
                "observations": metrics.observations,
                "fixed": metrics.fixed,
                "fix rate": f"{metrics.fix_rate:.1%}",
                "mean attempts": round(metrics.mean_attempts, 2),
                "regressions": metrics.regressions_introduced,
                "weakened": metrics.weakened,
                "weakening rate": f"{metrics.weakening_rate:.1%}",
                "cost/fix": (
                    f"${metrics.cost_per_fix:.4f}" if metrics.fixed else "n/a"
                ),
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.markdown(render_markdown(result.metrics_by_arm, result.reports_by_arm))

    output_dir = Path(result.output_dir) if result.output_dir else None
    if output_dir is not None:
        columns = st.columns(3)
        for column, name, mime in (
            (columns[0], "observations.csv", "text/csv"),
            (columns[1], "benchmark.json", "application/json"),
            (columns[2], "benchmark.md", "text/markdown"),
        ):
            path = output_dir / name
            if path.is_file():
                column.download_button(
                    f"Download {name}",
                    data=path.read_bytes(),
                    file_name=name,
                    mime=mime,
                    width="stretch",
                )
    st.caption(
        "observations.csv is one row per failing test per arm, paired on "
        "(project, test id) -- the file to run McNemar's test on."
    )
    _render_log()


def _resolve_spec_source(spec):
    """Let a manifest hold repo-relative paths as well as absolute ones."""
    source = spec.source
    if not source.startswith(("http://", "https://", "git@")):
        candidate = Path(source)
        if not candidate.is_absolute():
            local = (REPO_ROOT / source).resolve()
            if local.exists():
                spec.source = str(local)
    return spec


# ---------------------------------------------------------------------------


def main() -> None:
    st.title("AUTEF v2 - diagnosed unit-test repair")
    st.caption(
        "Upload a .zip, paste a GitHub link, or point at a directory. The "
        "layout, test roots and dependencies are detected from the project "
        "itself; failures are diagnosed before they are repaired, and every "
        "repair is re-run and checked for weakened assertions before it is "
        "accepted."
    )

    _sidebar()
    repair_tab, compare_tab, benchmark_tab = st.tabs(
        ["Repair a project", "Compare v1 vs v2", "Benchmark"]
    )
    with repair_tab:
        _repair_tab()
    with compare_tab:
        _compare_tab()
    with benchmark_tab:
        _benchmark_tab()


if __name__ == "__main__":  # `streamlit run` executes this as __main__
    main()
