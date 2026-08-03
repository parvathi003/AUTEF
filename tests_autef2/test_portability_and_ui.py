"""Tests for the additions that let arbitrary projects be uploaded and driven.

Each group is regression cover for a real project that could not be processed:

* **collection failures** -- a suite whose test files fail to import reports a
  repairable failure, not an unrunnable project. This is cover for a bug that
  made the benchmark return zero observations: a seeded broken import killed
  collection, the suite was declared not to have run, and every arm scored
  nothing.
* **remote and packed sources** -- GitHub links and tarballs resolve to the
  same layout a directory would.
* **test discovery** -- Django's per-app ``tests.py`` is found *and* collected.
* **test root isolation** -- Flask ships ``examples/*/tests`` for uninstalled
  demo projects; one unimportable conftest must not erase the 479 tests that do
  run.
* **pytest pinning** -- Flask locks pytest 9.0.3 and its conftest uses a private
  name removed in 9.1, so installing the newest pytest errors the whole suite.
* **long paths** -- django-crispy-forms' nested fixtures pass Windows' 260
  character limit and used to crash ingest with a bare FileNotFoundError.
* **the stagewise UI** -- stages 1 to 3 drive the real pipeline, so the app
  cannot silently diverge from the CLI. No model is involved.
"""

import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from autef2.config import AutefConfig
from autef2.ingest import (
    NON_STANDARD_TEST_FILES,
    TEST_FILE_RE,
    IngestError,
    _GITHUB_RE,
    _native,
    analyse,
    ingest,
)
from autef2.models import Outcome, SuiteResult, TestFailure
from autef2.runner import TestRunner, _from_longrepr
from autef2.venv_manager import pinned_pytest, prepare_environment

from conftest import SAMPLE_PROJECT

# A collection failure as pytest renders it: no structured crash, no traceback
# entries, everything in the text.
COLLECT_LONGREPR = """\
ImportError while importing test module 'C:\\ws\\tests\\test_operations.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\\Python312\\Lib\\importlib\\__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests\\test_operations.py:10: in <module>
    from calc.operations_missing import Calculator
E   ModuleNotFoundError: No module named 'calc.operations_missing'
"""


def make_config(tmp_path) -> AutefConfig:
    return AutefConfig(
        workspace=tmp_path / "ws", use_venv=False, api_key="stub"
    )


# ---------------------------------------------------------------------------
# collection failures
# ---------------------------------------------------------------------------


def test_reported_counts_collection_errors():
    """``total`` counts tests; ``reported`` is what pytest told us about."""
    error = TestFailure(nodeid="tests/test_x.py", outcome=Outcome.ERROR)
    result = SuiteResult(collection_errors=[error])
    assert result.total == 0
    assert result.reported == 1


def test_longrepr_yields_cause_and_frames():
    exception_type, message, frames = _from_longrepr(COLLECT_LONGREPR)

    assert exception_type == "ModuleNotFoundError"
    assert message == "No module named 'calc.operations_missing'"
    # Innermost frame last, matching pytest's own ordering, so the resolver's
    # "deepest frame wins" scoring still holds.
    assert [(Path(f.path).name, f.lineno) for f in frames] == [
        ("__init__.py", 90),
        ("test_operations.py", 10),
    ]


def test_longrepr_of_empty_text_is_harmless():
    assert _from_longrepr("") == ("", "", [])


def test_import_broken_suite_is_runnable_and_diagnosable(tmp_path):
    """The whole-suite import failure case, end to end through pytest."""
    config = make_config(tmp_path)
    project = tmp_path / "project"
    _copy_sample(project)

    test_file = project / "tests" / "test_operations.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace(
            "from calc.operations import Calculator",
            "from calc.operations_missing import Calculator",
        ),
        encoding="utf-8",
    )

    layout = ingest(str(project), config, name_hint="broken")
    environment = prepare_environment(layout, config)
    result = TestRunner(layout, environment, config).run_suite()

    assert result.ran, "an import error is a repairable failure, not a dead suite"
    assert result.total == 0 and len(result.collection_errors) == 1

    from autef2.resolver import resolve_all

    failure = resolve_all(result.collection_errors, layout)[0]
    assert failure.exception_type == "ModuleNotFoundError"
    assert Path(failure.test_file).name == "test_operations.py"


def test_collection_failures_do_not_share_one_signature():
    """Without a cause, every import crash hashed the same and the cache would
    reuse one strategy across unrelated failures."""
    first = TestFailure(
        nodeid="tests/test_a.py",
        outcome=Outcome.ERROR,
        exception_type="ModuleNotFoundError",
        exception_message="No module named 'alpha'",
    )
    second = TestFailure(
        nodeid="tests/test_b.py",
        outcome=Outcome.ERROR,
        exception_type="SyntaxError",
        exception_message="invalid syntax",
    )
    assert first.signature() != second.signature()


# ---------------------------------------------------------------------------
# remote and packed sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, owner, repo, ref",
    [
        ("https://github.com/benjaminp/six", "benjaminp", "six", None),
        ("https://github.com/psf/requests.git", "psf", "requests", None),
        ("http://github.com/a/b/", "a", "b", None),
        ("https://github.com/mahmoud/boltons/tree/master", "mahmoud", "boltons", "master"),
        ("https://github.com/o/r/tree/feature/x", "o", "r", "feature/x"),
    ],
)
def test_github_urls_are_understood(url, owner, repo, ref):
    match = _GITHUB_RE.match(url)
    assert match is not None
    assert (match.group("owner"), match.group("repo"), match.group("ref")) == (
        owner, repo, ref,
    )


def test_non_repository_url_is_refused(tmp_path):
    with pytest.raises(IngestError, match="GitHub project URL"):
        ingest("https://example.com/not-a-project", make_config(tmp_path))


def test_tarball_is_ingested_like_a_directory(tmp_path):
    """PyPI sdists arrive as .tar.gz, not .zip."""
    config = make_config(tmp_path)
    archive = tmp_path / "calcpkg-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(SAMPLE_PROJECT, arcname="calcpkg-1.0")

    layout = ingest(str(archive), config)

    assert layout.test_roots, "tests should have been found inside the tarball"
    assert layout.layout_style == "src"
    assert any(Path(f).name == "test_operations.py" for f in layout.test_files)


# ---------------------------------------------------------------------------
# test discovery: Django's tests.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, matches",
    [
        ("test_operations.py", True),
        ("operations_test.py", True),
        ("tests.py", True),      # Django app convention
        ("test.py", True),       # single-file convention
        ("conftest.py", False),
        ("testing.py", False),   # a module about testing, not a test module
        ("latest.py", False),
    ],
)
def test_which_filenames_count_as_tests(name, matches):
    assert bool(TEST_FILE_RE.match(name)) is matches


def _django_style_project(root: Path) -> Path:
    """An app package whose tests live in tests.py, as manage.py expects."""
    (root / "polls").mkdir(parents=True)
    (root / "polls" / "__init__.py").write_text("", encoding="utf-8")
    (root / "polls" / "models.py").write_text(
        "def shout(text):\n    return text.upper()\n", encoding="utf-8"
    )
    (root / "polls" / "tests.py").write_text(
        "from polls.models import shout\n"
        "\n"
        "\n"
        "def test_shout():\n"
        "    assert shout('why') == 'WHY'\n",
        encoding="utf-8",
    )
    return root


def test_tests_py_is_discovered(tmp_path):
    layout = analyse(_django_style_project(tmp_path / "site"))

    assert [Path(f).name for f in layout.test_files] == ["tests.py"]
    assert any("tests.py/test.py style" in note for note in layout.notes)


def test_tests_py_is_actually_collected(tmp_path):
    """Discovery is pointless if pytest then refuses to collect the file."""
    config = make_config(tmp_path)
    layout = ingest(str(_django_style_project(tmp_path / "site")), config)
    environment = prepare_environment(layout, config)

    result = TestRunner(layout, environment, config).run_suite()

    assert result.ran
    assert len(result.passed) == 1, result.stdout_tail


def test_python_files_is_only_widened_when_needed(tmp_path):
    """Overriding python_files replaces the project's own setting, so it is
    done only for projects that actually use the other convention."""
    config = make_config(tmp_path)
    environment = prepare_environment(analyse(SAMPLE_PROJECT), config)

    standard = TestRunner(analyse(SAMPLE_PROJECT), environment, config)
    assert standard._python_files() is None

    django_layout = analyse(_django_style_project(tmp_path / "site"))
    widened = TestRunner(django_layout, environment, config)._python_files()
    assert widened is not None
    assert "tests.py" in widened
    # pytest's own defaults must survive the override.
    assert "test_*.py" in widened and "*_test.py" in widened


# ---------------------------------------------------------------------------
# test root isolation
# ---------------------------------------------------------------------------


def test_one_unimportable_test_root_does_not_erase_the_others(tmp_path):
    """Flask's case: examples/*/tests belong to uninstalled demo projects."""
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    (root / "examples" / "demo" / "tests").mkdir(parents=True)
    (root / "examples" / "demo" / "tests" / "conftest.py").write_text(
        "import a_module_that_is_not_installed  # noqa: F401\n", encoding="utf-8"
    )
    (root / "examples" / "demo" / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n", encoding="utf-8"
    )

    config = make_config(tmp_path)
    layout = ingest(str(root), config)
    assert len(layout.test_roots) == 2, layout.test_roots

    result = TestRunner(layout, prepare_environment(layout, config), config).run_suite()

    assert result.ran, "a broken root must not declare the whole project dead"
    assert len(result.passed) == 2
    assert "could not be executed" in result.stdout_tail


# ---------------------------------------------------------------------------
# pytest pinning
# ---------------------------------------------------------------------------


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return root


def test_pin_from_a_test_requirements_file(tmp_path):
    root = _write(tmp_path / "p", "requirements.txt", "pytest==8.3.4\ndjango>=5\n")
    assert pinned_pytest(analyse(root)) == "pytest==8.3.4"


def test_pin_from_an_optional_dependencies_extra(tmp_path):
    root = _write(
        tmp_path / "p",
        "pyproject.toml",
        '[project]\nname = "p"\nversion = "1.0"\n'
        '[project.optional-dependencies]\ntest = ["pytest>=7,<8", "coverage"]\n',
    )
    assert pinned_pytest(analyse(root)) == "pytest>=7,<8"


def test_pin_from_a_lockfile_when_the_declaration_is_bare(tmp_path):
    """Flask's exact case: a bare "pytest" declared, an exact version locked."""
    root = tmp_path / "p"
    _write(
        root,
        "pyproject.toml",
        '[project]\nname = "p"\nversion = "1.0"\n'
        '[dependency-groups]\ndev = ["pytest"]\n',
    )
    _write(
        root,
        "uv.lock",
        'version = 1\n\n[[package]]\nname = "werkzeug"\nversion = "3.1.3"\n\n'
        '[[package]]\nname = "pytest"\nversion = "9.0.3"\n',
    )
    assert pinned_pytest(analyse(root)) == "pytest==9.0.3"


def test_pytest_plugins_are_not_mistaken_for_pytest(tmp_path):
    root = _write(
        tmp_path / "p", "requirements.txt", "pytest-django==4.8\npytest_cov==5.0\n"
    )
    assert pinned_pytest(analyse(root)) is None


def test_no_declaration_means_no_pin(tmp_path):
    root = _write(tmp_path / "p", "requirements.txt", "django>=5\n")
    assert pinned_pytest(analyse(root)) is None


# ---------------------------------------------------------------------------
# long paths
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the 260 character limit is Windows'")
def test_native_paths_carry_the_extended_prefix():
    prefixed = _native(Path(r"C:\some\where\deep"))
    assert str(prefixed).startswith("\\\\?\\C:\\")
    # Already-prefixed paths are left alone rather than doubled up.
    assert _native(prefixed) == prefixed


@pytest.mark.skipif(os.name != "nt", reason="the 260 character limit is Windows'")
def test_unc_paths_get_the_unc_form():
    assert str(_native(Path(r"\\server\share\project"))) == (
        "\\\\?\\UNC\\server\\share\\project"
    )


def test_deeply_nested_archive_unpacks(tmp_path):
    """django-crispy-forms nests fixtures far enough to pass 260 characters."""
    deep = "/".join(f"level_{i:02d}_of_a_nested_fixture_tree" for i in range(7))
    archive = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("proj/tests/test_x.py", "def test_x():\n    assert True\n")
        handle.writestr(f"proj/tests/{deep}/fixture.html", "<p>fixture</p>")

    # A workspace that is itself nested, which is what turns a long relative
    # path into an unwritable absolute one.
    padded = tmp_path / ("w_" + "x" * 40) / ("w_" + "y" * 40)
    config = AutefConfig(workspace=padded, use_venv=False, api_key="stub")

    layout = ingest(str(archive), config)

    assert [Path(f).name for f in layout.test_files] == ["test_x.py"]
    fixture = Path(layout.root) / "tests" / deep / "fixture.html"
    assert _native(fixture).is_file(), "the deep member should have been written"


# ---------------------------------------------------------------------------
# diagnosing and repairing as two steps
# ---------------------------------------------------------------------------


GOOD_MULTIPLY = """
def test_multiply_returns_product(self):
    self.assertEqual(self.calc.multiply(3, 4), 12)
"""


def _scripted_orchestrator(tmp_path, replies):
    """Real pipeline, scripted model -- the arrangement test_pipeline uses."""
    from autef2.llm import StubLLMClient
    from autef2.orchestrator import RepairOrchestrator
    from autef2.resolver import resolve_all
    from autef2.runner import TestRunner

    config = AutefConfig(
        workspace=tmp_path / "ws",
        use_venv=False,
        use_signature_cache=False,
        api_key="stub",
        max_attempts=3,
    )
    diagnoses = {"count": 0}

    def responder(messages):
        if "test-failure analyst" in messages[0]["content"]:
            diagnoses["count"] += 1
            return json.dumps(
                {
                    "root_cause": "assertion_mismatch",
                    "at_fault": "test",
                    "confidence": 0.9,
                    "explanation": "scripted",
                    "evidence": [],
                }
            )
        user = messages[-1]["content"]
        for name, pending in replies.items():
            if name in user:
                return pending.pop(0) if pending else "NO_TEST_FIX_NEEDED"
        return "NO_TEST_FIX_NEEDED"

    llm = StubLLMClient(config, responder=responder)
    layout = ingest(SAMPLE_PROJECT, config)
    environment = prepare_environment(layout, config)
    orchestrator = RepairOrchestrator(layout, environment, config, llm)

    before = TestRunner(layout, environment, config).run_suite()
    failures = resolve_all(list(before.failures), layout)
    return orchestrator, failures, before, diagnoses


def test_diagnosis_is_reused_when_repair_follows_it(tmp_path):
    """The stagewise UI diagnoses first, then repairs; that must not re-diagnose."""
    orchestrator, failures, before, diagnoses = _scripted_orchestrator(
        tmp_path, {"test_multiply_returns_product": [GOOD_MULTIPLY]}
    )
    failure = next(f for f in failures if "multiply" in f.nodeid)

    record = orchestrator.diagnose(failure)
    assert record.diagnosis is not None
    assert diagnoses["count"] == 1

    repaired = orchestrator.repair(failure, before.passed, record=record)

    assert repaired.fixed
    assert repaired.diagnosis is record.diagnosis
    assert diagnoses["count"] == 1, "repair must not diagnose the same failure twice"


def test_repair_diagnoses_for_itself_when_given_no_record(tmp_path):
    """The CLI path, unchanged: repair alone still does its own diagnosis."""
    orchestrator, failures, before, diagnoses = _scripted_orchestrator(
        tmp_path, {"test_multiply_returns_product": [GOOD_MULTIPLY]}
    )
    failure = next(f for f in failures if "multiply" in f.nodeid)

    record = orchestrator.repair(failure, before.passed)

    assert record.fixed
    assert record.diagnosis is not None
    assert diagnoses["count"] == 1


# ---------------------------------------------------------------------------
# the uploaded project is never worked on in place
# ---------------------------------------------------------------------------


def test_a_directory_outside_the_workspace_is_always_copied(tmp_path):
    """The bug this pins: everything downstream -- seeding faults, patching
    tests -- writes to ``layout.root``. If ingest ever hands back the caller's
    own directory, a comparison run edits the user's source tree. It did: a
    name_hint carrying an absolute path made ``projects_dir / name_hint``
    collapse onto the original, and the fixture project was seeded with a fault
    in place."""
    original = _django_style_project(tmp_path / "their_project")
    before = (original / "polls" / "tests.py").read_text(encoding="utf-8")
    config = make_config(tmp_path)

    layout = ingest(str(original), config, name_hint=str(original))

    assert Path(layout.root).resolve() != original.resolve()
    assert Path(layout.root).resolve().is_relative_to(
        config.projects_dir.resolve()
    ), "the working copy must live in the workspace"

    # Prove it by writing through the layout the way the repair loop does.
    Path(layout.root, "polls", "tests.py").write_text("# patched\n", encoding="utf-8")
    assert (original / "polls" / "tests.py").read_text(encoding="utf-8") == before


def test_a_project_already_in_the_workspace_is_used_in_place(tmp_path):
    """The flip side: re-ingesting a workspace copy must not copy it again,
    otherwise a seeded fault would be discarded between arms."""
    config = make_config(tmp_path)
    first = ingest(str(_django_style_project(tmp_path / "src_project")), config)

    second = ingest(first.root, config)

    assert Path(second.root).resolve() == Path(first.root).resolve()


@pytest.mark.parametrize(
    "source, expected",
    [
        ("https://github.com/owner/repo", "repo"),
        ("https://github.com/owner/repo.git", "repo"),
        ("https://github.com/owner/repo/", "repo"),
        ("tests_autef2/sample_project", "sample_project"),
        (r"C:\Users\me\Documents\project\sample_project", "sample_project"),
        (r"C:\Users\me\Documents\project\sample_project\\", "sample_project"),
        ("/home/me/work/sample_project", "sample_project"),
    ],
)
def test_project_labels_are_bare_names(source, expected):
    """A label becomes ingest's name_hint, so it must never carry separators."""
    from autef2.eval.compare import _label

    label = _label(source)
    assert label == expected
    assert "/" not in label and "\\" not in label


# ---------------------------------------------------------------------------
# v1 versus v2 comparison
# ---------------------------------------------------------------------------


def _record(nodeid, *, fixed, attempts=1, tokens=100, skipped=None, cause=None):
    from autef2.models import Diagnosis, RepairAttempt, RepairRecord, RootCause

    record = RepairRecord(nodeid=nodeid, signature="sig", fixed=fixed)
    record.skipped_reason = skipped
    if cause is not None:
        record.diagnosis = Diagnosis(root_cause=cause, confidence=0.9, at_fault="test")
    for index in range(attempts):
        record.attempts.append(
            RepairAttempt(
                attempt=index + 1,
                strategy_id=f"s{index}",
                strategy_label="scripted",
                verified_pass=fixed and index == attempts - 1,
                prompt_tokens=tokens,
            )
        )
    return record


def _report(arm, records, **kwargs):
    from autef2.models import RunReport, SuiteResult

    report = RunReport(project="p", arm=arm, records=list(records), **kwargs)
    report.before = SuiteResult(passed=["kept"])
    report.after = SuiteResult(passed=["kept"])
    return report


def test_paired_counts_form_mcnemars_table():
    from autef2.eval.compare import BASELINE, CANDIDATE, from_benchmark
    from autef2.eval.benchmark import BenchmarkResult
    from autef2.eval.metrics import compute_metrics

    baseline_records = [
        _record("t_both", fixed=True),
        _record("t_v2_only", fixed=False),
        _record("t_v1_only", fixed=True),
        _record("t_neither", fixed=False),
    ]
    candidate_records = [
        _record("t_both", fixed=True),
        _record("t_v2_only", fixed=True, attempts=2),
        _record("t_v1_only", fixed=False),
        _record("t_neither", fixed=False),
    ]
    reports = {
        BASELINE: [_report(BASELINE, baseline_records)],
        CANDIDATE: [_report(CANDIDATE, candidate_records)],
    }
    bench = BenchmarkResult(
        reports_by_arm=reports,
        metrics_by_arm={
            arm: compute_metrics(arm, rs) for arm, rs in reports.items()
        },
    )

    result = from_benchmark(bench, project="p")

    assert result.counts.both == 1
    assert result.counts.candidate_only == 1
    assert result.counts.baseline_only == 1
    assert result.counts.neither == 1
    assert {t.nodeid: t.verdict for t in result.tests} == {
        "t_both": "both",
        "t_v2_only": "v2 only",
        "t_v1_only": "v1 only",
        "t_neither": "neither",
    }


@pytest.mark.parametrize(
    "candidate_only, baseline_only, expected",
    [
        (0, 0, None),      # no discordant pairs: no evidence either way
        (1, 0, 1.0),       # 2 * (1/2)
        (4, 0, 0.125),     # 2 * (1/16)
        (7, 0, 0.015625),  # 2 * (1/128)
        (2, 2, 1.0),       # perfectly split
    ],
)
def test_exact_mcnemar_p_value(candidate_only, baseline_only, expected):
    from autef2.eval.compare import PairedCounts

    counts = PairedCounts(
        candidate_only=candidate_only, baseline_only=baseline_only
    )
    if expected is None:
        assert counts.p_value is None
    else:
        assert counts.p_value == pytest.approx(expected)


def test_observations_the_arm_never_put_to_the_model_are_separated():
    """v1 cannot attempt a module-level failure; that is not a prompt result."""
    from autef2.eval.metrics import compute_metrics

    records = [
        _record("attempted_and_fixed", fixed=True, tokens=120),
        _record("attempted_and_missed", fixed=False, tokens=120),
        # An attempt that spent nothing: the failing function was not locatable.
        _record("unreachable", fixed=False, tokens=0),
    ]
    metrics = compute_metrics("baseline", [_report("baseline", records)])

    assert metrics.observations == 3
    assert metrics.model_attempted == 2
    assert metrics.no_attempt == 1
    # 1 of 3 offered, but 1 of the 2 it could actually try.
    assert metrics.fix_rate_all == pytest.approx(1 / 3)
    assert metrics.fix_rate_attempted == pytest.approx(0.5)


def test_efficiency_is_reported_per_fix():
    from autef2.eval.metrics import compute_metrics

    report = _report(
        "autef2",
        [_record("a", fixed=True), _record("b", fixed=True)],
        prompt_tokens=800, completion_tokens=200, llm_calls=6,
        cost_usd=0.02, duration_s=60.0,
    )
    metrics = compute_metrics("autef2", [report])

    assert metrics.fixed == 2
    assert metrics.tokens_per_fix == pytest.approx(500)
    assert metrics.calls_per_fix == pytest.approx(3)
    assert metrics.seconds_per_fix == pytest.approx(30)
    assert metrics.cost_per_fix == pytest.approx(0.01)


def test_a_fallback_diagnosis_does_not_claim_the_model_disagreed(tmp_path):
    """The Repair tab looked like it was working while the quota was exhausted:
    stage 4 showed plausible root causes, because diagnosis falls back to the
    static classifier. Nothing said the model had never answered."""
    from autef2.agents.failure_analysis import FailureAnalysisAgent
    from autef2.llm import LLMError, StubLLMClient
    from autef2.models import Outcome, RootCause

    config = make_config(tmp_path)

    def refuse(_messages):
        raise LLMError("Error code: 429 - insufficient_quota")

    failure = TestFailure(
        nodeid="tests/test_x.py::test_y",
        outcome=Outcome.FAILED,
        exception_type="AttributeError",
        exception_message="'Calculator' object has no attribute 'description'",
    )
    layout = analyse(SAMPLE_PROJECT)

    diagnosis = FailureAnalysisAgent(
        StubLLMClient(config, responder=refuse), config
    ).diagnose(failure, layout, "context")

    # The heuristic still produces a usable label...
    assert diagnosis.root_cause is not RootCause.UNKNOWN
    # ...but it must be marked as not coming from the model.
    assert diagnosis.model_answered is False
    assert "model unavailable" in diagnosis.explanation


def test_a_real_diagnosis_is_marked_as_answered(tmp_path):
    """The flag must not fire for an ordinary model-produced diagnosis."""
    from autef2.agents.failure_analysis import FailureAnalysisAgent
    from autef2.llm import StubLLMClient
    from autef2.models import Outcome

    config = make_config(tmp_path)
    reply = json.dumps(
        {
            "root_cause": "assertion_mismatch",
            "at_fault": "test",
            "confidence": 0.9,
            "explanation": "scripted",
            "evidence": [],
        }
    )
    failure = TestFailure(
        nodeid="tests/test_x.py::test_y",
        outcome=Outcome.FAILED,
        exception_type="AssertionError",
        exception_message="12 != 14",
    )

    diagnosis = FailureAnalysisAgent(
        StubLLMClient(config, responder=lambda m: reply), config
    ).diagnose(failure, analyse(SAMPLE_PROJECT), "context")

    assert diagnosis.model_answered is True


def test_the_summary_says_when_the_model_was_never_reached(tmp_path):
    """`repaired 0/2` on its own reads as "the agents tried and failed"."""
    from autef2.models import RepairAttempt, RepairRecord, RunReport, SuiteResult
    from autef2.pipeline import summarise

    record = RepairRecord(nodeid="tests/test_x.py::test_y", signature="s")
    record.attempts.append(
        RepairAttempt(
            attempt=1,
            strategy_id="align_expected_value",
            strategy_label="align",
            rejected_reason="model call failed: Error code: 429 - insufficient_quota",
        )
    )
    report = RunReport(project="p", records=[record])
    report.before = SuiteResult(failures=[])
    report.after = SuiteResult()

    text = summarise(report)

    assert "never reached" in text
    assert "insufficient_quota" in text


def test_a_run_where_every_model_call_failed_is_not_a_comparison():
    """Zeros in a comparison table are indistinguishable from a measured tie.

    An exhausted API quota produced exactly this: eight failing tests offered,
    nothing put to the model, and a tidy table of 0s and "same" that reads as
    "the two versions performed identically".
    """
    from autef2.eval.benchmark import BenchmarkResult
    from autef2.eval.compare import BASELINE, CANDIDATE, from_benchmark, render_comparison
    from autef2.eval.metrics import compute_metrics
    from autef2.models import RepairAttempt, RepairRecord, RunReport, SuiteResult

    quota = (
        "model call failed: LLM call failed after 3 attempts: Error code: 429 - "
        "insufficient_quota"
    )

    def failed_report(arm):
        records = []
        for index in range(8):
            record = RepairRecord(nodeid=f"tests/test_x.py::test_{index}", signature="s")
            record.attempts.append(
                RepairAttempt(
                    attempt=1,
                    strategy_id="align_expected_value",
                    strategy_label="scripted",
                    rejected_reason=quota,
                )
            )
            records.append(record)
        report = RunReport(project="p", arm=arm, records=records)
        report.before = SuiteResult(passed=["kept"])
        report.after = SuiteResult(passed=["kept"])
        return report

    reports = {BASELINE: [failed_report(BASELINE)], CANDIDATE: [failed_report(CANDIDATE)]}
    bench = BenchmarkResult(
        reports_by_arm=reports,
        metrics_by_arm={arm: compute_metrics(arm, rs) for arm, rs in reports.items()},
    )

    result = from_benchmark(bench, project="p")

    assert result.model_unavailable
    assert "No model call succeeded" in (result.error or "")
    assert "insufficient_quota" in (result.error or "")

    text = render_comparison(result)
    assert "No comparison can be made" in text
    # The tables must not be printed: they would show a tie that was never measured.
    assert "Fix rate (of attempted)" not in text
    assert "Cost per fix" not in text


def test_a_run_where_the_model_answered_is_still_reported_normally():
    """The guard must not suppress a real result that happens to fix nothing."""
    from autef2.eval.benchmark import BenchmarkResult
    from autef2.eval.compare import BASELINE, CANDIDATE, from_benchmark, render_comparison
    from autef2.eval.metrics import compute_metrics

    reports = {
        BASELINE: [_report(BASELINE, [_record("t", fixed=False, tokens=120)])],
        CANDIDATE: [_report(CANDIDATE, [_record("t", fixed=True, tokens=120)])],
    }
    bench = BenchmarkResult(
        reports_by_arm=reports,
        metrics_by_arm={arm: compute_metrics(arm, rs) for arm, rs in reports.items()},
    )

    result = from_benchmark(bench, project="p")

    assert not result.model_unavailable
    assert "Fix rate (of attempted)" in render_comparison(result)


def test_comparison_report_names_what_v1_could_not_reach():
    from autef2.eval.benchmark import BenchmarkResult
    from autef2.eval.compare import BASELINE, CANDIDATE, from_benchmark, render_comparison
    from autef2.eval.metrics import compute_metrics
    from autef2.models import RootCause

    reports = {
        BASELINE: [
            _report(
                BASELINE,
                [_record("tests/test_x.py", fixed=False, tokens=0)],
                cost_usd=0.0,
            )
        ],
        CANDIDATE: [
            _report(
                CANDIDATE,
                [
                    _record(
                        "tests/test_x.py", fixed=True, attempts=2, tokens=200,
                        cause=RootCause.IMPORT_ERROR,
                    )
                ],
                prompt_tokens=400, completion_tokens=100, llm_calls=3,
                cost_usd=0.004, duration_s=20.0,
            )
        ],
    }
    bench = BenchmarkResult(
        reports_by_arm=reports,
        metrics_by_arm={arm: compute_metrics(arm, rs) for arm, rs in reports.items()},
    )

    text = render_comparison(from_benchmark(bench, project="demo"))

    assert "v1 never attempted 1 of 1" in text
    assert "Fix rate (of attempted)" in text
    assert "import_error" in text
    # The efficiency section must not claim a per-fix win for an arm with none.
    assert "Cost per fix" in text


# ---------------------------------------------------------------------------
# the stagewise UI
# ---------------------------------------------------------------------------


@pytest.fixture
def app_test():
    streamlit_testing = pytest.importorskip("streamlit.testing.v1")
    ui = Path(__file__).resolve().parent.parent / "src" / "autef2" / "ui.py"
    return streamlit_testing.AppTest.from_file(str(ui), default_timeout=180)


def test_app_starts_clean(app_test):
    app_test.run()
    assert not app_test.exception


def test_app_shows_one_numbered_sequence(app_test, tmp_path):
    """v1's phases are stages of this pipeline, not a side menu.

    Generation, coverage and mutation used to sit in their own row that "Run
    all stages" skipped, which read as a repair tool with extras bolted on.
    They are stages 4, 7 and 8 of one sequence now, and the order is load
    bearing: generation feeds the repair loop, and mutation refuses to score a
    suite that is not green, so it has to follow repair.
    """
    project = tmp_path / "project"
    _copy_sample(project)

    app_test.run()
    app_test.radio[0].set_value("Path on this machine").run()
    app_test.text_input[0].set_value(str(project)).run()
    assert not app_test.exception

    numbered = [b.label for b in app_test.button if b.label[:1].isdigit()]
    assert numbered == [
        "1. Ingest",
        "2. Environment",
        "3. Run suite",
        "4. Generate tests",
        "5. Diagnose",
        "6. Repair & verify",
        "7. Coverage",
        "8. Mutation",
        "9. Report",
    ]


def test_app_stages_one_to_three_run_without_a_model(app_test, tmp_path):
    """The scope check works with no API key, which is the point of stage 3."""
    project = tmp_path / "project"
    _copy_sample(project)

    app_test.run()
    app_test.radio[0].set_value("Path on this machine").run()
    app_test.text_input[0].set_value(str(project)).run()
    assert not app_test.exception

    for label, next_label in (
        ("1. Ingest", "2. Environment"),
        ("2. Environment", "3. Run suite"),
        ("3. Run suite", "4. Generate tests"),
    ):
        _click(app_test, label).run()
        assert not app_test.exception, f"stage {label} raised"
        # The button row is drawn before the stage runs, so without a re-run
        # the next stage stays disabled and the user is stuck on a dead button.
        assert not _button(app_test, next_label).disabled, (
            f"{next_label} should be enabled once {label} has produced its state"
        )

    # Generation is stage 4, but it is not the only thing stage 3 unlocks: the
    # repair path stays reachable for a project that needs no new tests.
    assert not _button(app_test, "5. Diagnose").disabled

    state = app_test.session_state
    assert state["layout"].layout_style == "src"
    assert state["environment"].python
    # The fixture's suite has two passing and two deliberately broken tests.
    assert len(state["before"].passed) == 2
    assert len(state["failures"]) == 2
    assert all(f.test_file for f in state["failures"]), "test files must resolve"


def test_app_says_a_project_has_no_tests_rather_than_nothing_to_fix(
    app_test, tmp_path
):
    """A repo with no test suite is not a repo whose tests all pass.

    MemerSala's case: three .py files, no tests. Reporting "nothing is failing"
    reads as success and sends the user looking for a bug that is not there.
    """
    project = tmp_path / "webapp"
    project.mkdir()
    (project / "app.py").write_text("def index():\n    return 'hi'\n", encoding="utf-8")

    app_test.run()
    app_test.radio[0].set_value("Path on this machine").run()
    app_test.text_input[0].set_value(str(project)).run()
    for label in ("1. Ingest", "2. Environment", "3. Run suite"):
        _click(app_test, label).run()

    assert not app_test.exception
    assert app_test.session_state["layout"].test_files == []
    warnings = " ".join(str(w.value) for w in app_test.warning)
    assert "no test files" in warnings.lower()
    assert "does not write new ones" in warnings
    assert not app_test.success, "an empty project is not a success state"


def test_app_refuses_repair_stages_without_a_key(app_test, tmp_path, monkeypatch):
    """A model-driven stage must explain that a key is needed, not raise.

    Checked on both of the stages that stage 3 unlocks: generation is the first
    one in the sequence, diagnosis the first of the repair loop.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # The machine running the tests may have a key configured; the behaviour
    # under test is what happens when it does not.
    monkeypatch.setattr("autef2.config.resolve_api_key", lambda: None)
    monkeypatch.setattr("autef2.ui.resolve_api_key", lambda: None)

    project = tmp_path / "project"
    _copy_sample(project)

    app_test.run()
    app_test.radio[0].set_value("Path on this machine").run()
    app_test.text_input[0].set_value(str(project)).run()
    for label in ("1. Ingest", "2. Environment", "3. Run suite"):
        _click(app_test, label).run()

    for label in ("4. Generate tests", "5. Diagnose"):
        _click(app_test, label).run()
        assert not app_test.exception
        assert any("API key" in str(element.value) for element in app_test.error), (
            f"{label} must say a key is needed rather than raising"
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _copy_sample(destination: Path) -> None:
    import shutil

    shutil.copytree(SAMPLE_PROJECT, destination)


def _button(app_test, label: str):
    for button in app_test.button:
        if button.label == label:
            return button
    raise AssertionError(f"no button labelled {label!r}")


def _click(app_test, label: str):
    return _button(app_test, label).click()
