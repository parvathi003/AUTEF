"""Unit tests for the pieces that must be right for the numbers to mean anything."""

import textwrap
from pathlib import Path

import pytest

from autef2 import guards, patcher
from autef2.agents.failure_analysis import heuristic_classify
from autef2.models import Frame, Outcome, RootCause, TestFailure
from autef2.runner import _split_exception

from conftest import SAMPLE_PROJECT


# ---------------------------------------------------------------------------
# guards: weakening detection
# ---------------------------------------------------------------------------

ORIGINAL = textwrap.dedent(
    """
    def test_multiply(self):
        result = self.calc.multiply(3, 4)
        self.assertEqual(result, 12)
    """
)


@pytest.mark.parametrize(
    "patched, expect_weak, expect_reason",
    [
        (
            "def test_multiply(self):\n    result = self.calc.multiply(3, 4)\n"
            "    self.assertEqual(result, 12)\n",
            False,
            None,
        ),
        (
            "def test_multiply(self):\n    self.calc.multiply(3, 4)\n",
            True,
            "removed",
        ),
        (
            "def test_multiply(self):\n    result = self.calc.multiply(3, 4)\n"
            "    self.assertTrue(True)\n",
            True,
            "trivially true",
        ),
        (
            "def test_multiply(self):\n    result = self.calc.multiply(3, 4)\n"
            "    self.assertIsNotNone(result)\n",
            True,
            "less specific",
        ),
        (
            "@unittest.skip('flaky')\ndef test_multiply(self):\n"
            "    result = self.calc.multiply(3, 4)\n"
            "    self.assertEqual(result, 12)\n",
            True,
            "skipped",
        ),
    ],
    ids=["genuine-fix", "assert-deleted", "assert-trivialised", "assert-broadened", "skipped"],
)
def test_weakening_detection(patched, expect_weak, expect_reason):
    report = guards.compare(ORIGINAL, patched)
    assert report.weakened is expect_weak
    if expect_reason:
        assert any(expect_reason in r for r in report.reasons), report.reasons


def test_bare_assert_counted():
    profile = guards.profile_function(
        "def test_x():\n    assert compute() == 42\n"
    )
    assert profile.total == 1
    assert profile.trivial == 0


def test_assert_true_is_trivial():
    profile = guards.profile_function("def test_x():\n    assert True\n")
    assert profile.trivial == 1


# ---------------------------------------------------------------------------
# patcher: AST surgery must not corrupt the file
# ---------------------------------------------------------------------------


def test_replaces_method_in_place_not_at_end_of_file(tmp_path):
    """v1 appended repaired methods at module level, silently unclassing them."""
    path = tmp_path / "test_thing.py"
    path.write_text(
        textwrap.dedent(
            """
            import unittest


            class TestThing(unittest.TestCase):
                def test_a(self):
                    self.assertEqual(1, 2)

                def test_b(self):
                    self.assertEqual(3, 3)
            """
        ).lstrip(),
        encoding="utf-8",
    )

    span = patcher.find_function(str(path), "test_a", "TestThing")
    assert span is not None

    patcher.replace_function(span, "def test_a(self):\n    self.assertEqual(1, 1)\n")

    text = path.read_text(encoding="utf-8")
    import ast

    tree = ast.parse(text)
    class_node = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    methods = [n.name for n in class_node.body]
    assert methods == ["test_a", "test_b"], "method must stay inside its class"
    assert "assertEqual(1, 1)" in text


def test_rejects_unparseable_replacement(tmp_path):
    path = tmp_path / "test_thing.py"
    path.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    span = patcher.find_function(str(path), "test_a")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(patcher.PatchError):
        patcher.replace_function(span, "def test_a(:\n    oops")

    assert path.read_text(encoding="utf-8") == before, "file must be untouched"


def test_rejects_renamed_function(tmp_path):
    path = tmp_path / "test_thing.py"
    path.write_text("def test_a():\n    assert False\n", encoding="utf-8")
    span = patcher.find_function(str(path), "test_a")

    with pytest.raises(patcher.PatchError, match="does not define"):
        patcher.replace_function(span, "def test_a_renamed():\n    assert True\n")


def test_strips_code_fences():
    assert patcher.strip_code_fences(
        "```python\ndef test_a():\n    pass\n```"
    ) == "def test_a():\n    pass"


def test_snapshot_restores_exactly(tmp_path):
    path = tmp_path / "f.py"
    path.write_text("original\n", encoding="utf-8")
    snapshot = patcher.Snapshot()
    snapshot.capture(str(path))
    path.write_text("clobbered\n", encoding="utf-8")
    snapshot.restore()
    assert path.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# diagnosis heuristic
# ---------------------------------------------------------------------------


def _failure(exc_type, message, phase="call"):
    return TestFailure(
        nodeid="tests/test_x.py::test_y",
        outcome=Outcome.FAILED,
        exception_type=exc_type,
        exception_message=message,
        longrepr=f"{exc_type}: {message}",
        phase=phase,
    )


@pytest.mark.parametrize(
    "failure, expected",
    [
        (_failure("ModuleNotFoundError", "No module named 'calc'"), RootCause.IMPORT_ERROR),
        (_failure("SyntaxError", "invalid syntax"), RootCause.COLLECTION_ERROR),
        (_failure("AssertionError", "12 != 14"), RootCause.ASSERTION_MISMATCH),
        (
            _failure("AssertionError", "Expected call not found: save(1)"),
            RootCause.MOCK_MISCONFIGURATION,
        ),
        (
            _failure("AttributeError", "Mock object has no attribute 'commit'"),
            RootCause.MOCK_MISCONFIGURATION,
        ),
        (
            _failure("TypeError", "add() takes 3 positional arguments but 4 were given"),
            RootCause.API_MISUSE,
        ),
        (
            _failure("ConnectionRefusedError", "Connection refused"),
            RootCause.ENVIRONMENT_DEPENDENCY,
        ),
        (_failure("Exception", "boom", phase="setup"), RootCause.FIXTURE_SETUP_ERROR),
        (_failure("Exception", "boom", phase="collect"), RootCause.COLLECTION_ERROR),
    ],
)
def test_heuristic_classification(failure, expected):
    cause, confidence = heuristic_classify(failure)
    assert cause is expected
    assert 0.0 <= confidence <= 1.0


def test_mock_signature_beats_generic_assertion_error():
    """Ordering matters: the mock rule must win over the AssertionError rule."""
    generic, _ = heuristic_classify(_failure("AssertionError", "12 != 14"))
    mocky, _ = heuristic_classify(
        _failure("AssertionError", "Expected 'save' to have been called once.")
    )
    assert generic is RootCause.ASSERTION_MISMATCH
    assert mocky is RootCause.MOCK_MISCONFIGURATION


# ---------------------------------------------------------------------------
# failure signatures
# ---------------------------------------------------------------------------


def test_signature_collapses_equivalent_failures():
    a = _failure("ModuleNotFoundError", "No module named 'alpha.beta'")
    b = _failure("ModuleNotFoundError", "No module named 'gamma.delta'")
    assert a.signature() == b.signature()


def test_signature_separates_different_failures():
    a = _failure("ModuleNotFoundError", "No module named 'alpha'")
    b = _failure("AssertionError", "12 != 14")
    assert a.signature() != b.signature()


# ---------------------------------------------------------------------------
# exception parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, exc_type, message",
    [
        ("AssertionError: 12 != 14", "AssertionError", "12 != 14"),
        ("assert 1 == 2", "AssertionError", "assert 1 == 2"),
        (
            "ModuleNotFoundError: No module named 'x'",
            "ModuleNotFoundError",
            "No module named 'x'",
        ),
        ("", "", ""),
    ],
)
def test_split_exception(raw, exc_type, message):
    assert _split_exception(raw) == (exc_type, message)


# ---------------------------------------------------------------------------
# ingest: layout detection without any hardcoded assumption
# ---------------------------------------------------------------------------


def test_detects_src_layout_and_name():
    from autef2.ingest import analyse

    layout = analyse(SAMPLE_PROJECT)
    assert layout.name == "calcpkg"  # from pyproject, not the directory name
    assert layout.layout_style == "src"
    assert layout.installable is True
    assert any(Path(r).name == "src" for r in layout.import_roots)
    assert len(layout.test_files) == 1


def test_detects_flat_layout(tmp_path):
    from autef2.ingest import analyse

    (tmp_path / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "test_thing.py").write_text(
        "from thing import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    layout = analyse(tmp_path)
    assert layout.layout_style == "flat"
    assert layout.installable is False
    assert len(layout.test_files) == 1


def test_ignores_committed_virtualenv(tmp_path):
    from autef2.ingest import analyse

    venv_tests = tmp_path / ".venv" / "Lib" / "site-packages" / "pkg"
    venv_tests.mkdir(parents=True)
    (venv_tests / "test_vendored.py").write_text("def test_v():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_real.py").write_text("def test_r():\n    pass\n", encoding="utf-8")

    layout = analyse(tmp_path)
    assert len(layout.test_files) == 1
    assert "test_real.py" in layout.test_files[0]


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


def test_resolves_test_file_from_nodeid_not_from_class_name(tmp_path):
    """The whole point: no CamelCase-to-filename guessing."""
    from autef2.ingest import analyse
    from autef2.resolver import resolve

    # A filename that shares nothing with the class name, which is exactly the
    # case v1's locate_test_file could not handle.
    (tmp_path / "checks_for_the_widget.py").write_text(
        "import unittest\n\n\nclass TestSomethingElse(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(1, 2)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    layout = analyse(tmp_path)

    failure = TestFailure(
        nodeid="checks_for_the_widget.py::TestSomethingElse::test_a",
        outcome=Outcome.FAILED,
        exception_type="AssertionError",
        exception_message="1 != 2",
    )
    resolve(failure, layout)

    assert failure.test_file is not None
    assert Path(failure.test_file).name == "checks_for_the_widget.py"
    assert failure.test_class == "TestSomethingElse"
    assert failure.test_function == "test_a"


def test_strips_parametrisation_from_function_name():
    from autef2.ingest import analyse
    from autef2.resolver import _parse_nodeid_parts

    assert _parse_nodeid_parts("t.py::TestA::test_b[case-1]") == ("TestA", "test_b")
    assert _parse_nodeid_parts("t.py::test_b[3-4]") == (None, "test_b")


def test_ignores_library_frames():
    from autef2.ingest import analyse
    from autef2.resolver import resolve

    layout = analyse(SAMPLE_PROJECT)
    failure = TestFailure(
        nodeid="tests/test_operations.py::TestCalculator::test_multiply_returns_product",
        outcome=Outcome.FAILED,
        exception_type="AssertionError",
        exception_message="12 != 14",
        frames=[
            Frame(path=str(SAMPLE_PROJECT / "tests" / "test_operations.py"), lineno=20),
            Frame(path="C:/Python312/Lib/site-packages/pluggy/_hooks.py", lineno=500),
            Frame(path=str(SAMPLE_PROJECT / "src" / "calc" / "operations.py"), lineno=9),
        ],
    )
    resolve(failure, layout)

    assert failure.source_files
    assert all("site-packages" not in p for p in failure.source_files)
    assert Path(failure.source_files[0]).name == "operations.py"


# ---------------------------------------------------------------------------
# fault injection
# ---------------------------------------------------------------------------


def test_injects_faults_that_are_syntactically_valid(tmp_path):
    import ast
    import shutil

    from autef2.eval.faults import FAULT_CAUSES, FaultInjector
    from autef2.ingest import analyse

    target = tmp_path / "project"
    shutil.copytree(SAMPLE_PROJECT, target)
    layout = analyse(target)

    records = FaultInjector(layout, seed=7).inject(4)
    assert records, "expected at least one seeded fault"

    for record in records:
        assert record.expected_cause in FAULT_CAUSES.values()
        # A seeded fault must break the test, not the file: an unparseable file
        # would collapse the whole suite into one collection error.
        ast.parse(Path(record.file).read_text(encoding="utf-8"))


def test_file_scoped_fault_never_shares_a_file(tmp_path):
    """A broken import collapses the file to one observation; keep it alone."""
    import shutil
    from collections import Counter

    from autef2.eval.faults import FILE_SCOPED_KINDS, FaultInjector
    from autef2.ingest import analyse

    target = tmp_path / "project"
    shutil.copytree(SAMPLE_PROJECT, target)
    layout = analyse(target)

    records = FaultInjector(layout, seed=7).inject(6)
    per_file = Counter(r.file for r in records)
    for record in records:
        if record.kind in FILE_SCOPED_KINDS:
            assert per_file[record.file] == 1, (
                f"{record.kind} shares {record.file} with "
                f"{per_file[record.file] - 1} other fault(s)"
            )


def test_a_single_test_file_does_not_collapse_to_one_import_fault(tmp_path):
    """File-scoped kinds must not crowd out everything on a small project.

    ``broken_import`` used to be taken first, claim the only test file, and
    every other fault popped for that file was discarded -- four requested
    faults became one collection error. That one observation is the failure v1
    *cannot attempt at all* (it replaces a failing test function, and a
    collection error has none), so the v1-vs-v2 gap was decided entirely by
    seeding order.
    """
    import shutil
    from collections import Counter

    from autef2.eval.faults import FILE_SCOPED_KINDS, FaultInjector
    from autef2.ingest import analyse

    target = tmp_path / "project"
    shutil.copytree(SAMPLE_PROJECT, target)
    layout = analyse(target)
    assert len(layout.test_files) == 1, "the fixture this test is about"

    injector = FaultInjector(layout, seed=7)
    records = injector.inject(4)

    assert len(records) >= 2, "one file still has room for several per-test faults"
    kinds = Counter(r.kind for r in records)
    assert not (set(kinds) & FILE_SCOPED_KINDS), (
        "a file-scoped fault took the only file and swallowed the rest: "
        f"{dict(kinds)}"
    )
    assert injector.shortfall, "seeding fewer than asked must be reported"
    assert "2 of 4" in injector.shortfall


def test_fault_kinds_can_be_restricted(tmp_path):
    """The mix is part of the protocol, so it has to be selectable."""
    import shutil

    from autef2.eval.faults import FaultInjector
    from autef2.ingest import analyse

    target = tmp_path / "project"
    shutil.copytree(SAMPLE_PROJECT, target)
    layout = analyse(target)

    records = FaultInjector(
        layout, seed=7, kinds=("wrong_expected",)
    ).inject(3)

    assert records
    assert {r.kind for r in records} == {"wrong_expected"}


def test_seeds_only_into_passing_tests(tmp_path):
    import shutil

    from autef2.eval.faults import FILE_SCOPED_KINDS, FaultInjector
    from autef2.ingest import analyse

    target = tmp_path / "project"
    shutil.copytree(SAMPLE_PROJECT, target)
    layout = analyse(target)

    passing = ["tests/test_operations.py::TestCalculator::test_add_returns_sum"]
    records = FaultInjector(layout, seed=3, passing_tests=passing).inject(6)

    for record in records:
        if record.kind in FILE_SCOPED_KINDS:
            continue
        assert record.enclosing_test == "test_add_returns_sum", (
            f"{record.kind} was seeded into {record.enclosing_test}, "
            "which was already failing"
        )


# ---------------------------------------------------------------------------
# signature cache
# ---------------------------------------------------------------------------


def test_cache_only_reuses_a_strategy_that_worked(tmp_path):
    from autef2.cache import SignatureCache

    cache = SignatureCache(tmp_path / "cache.json")
    assert cache.lookup("sig") is None

    cache.record_failure("sig", "fix_import_statement")
    assert cache.lookup("sig") is None, "a failure alone must never be reusable"

    cache.record_success("sig", RootCause.IMPORT_ERROR, "fix_import_statement")
    entry = cache.lookup("sig")
    assert entry is not None and entry.strategy_id == "fix_import_statement"

    # Once it fails more than it works, stop reusing it.
    cache.record_failure("sig", "fix_import_statement")
    cache.record_failure("sig", "fix_import_statement")
    assert cache.lookup("sig") is None


def test_cache_persists_across_instances(tmp_path):
    from autef2.cache import SignatureCache

    path = tmp_path / "cache.json"
    SignatureCache(path).record_success("sig", RootCause.IMPORT_ERROR, "fix_import_statement")
    assert SignatureCache(path).lookup("sig") is not None


def test_request_shape_adapts_to_a_reasoning_model():
    """A model that refuses the ordinary parameters is answered, not retried.

    Reasoning models take ``max_completion_tokens`` and reject any temperature
    but the default. We do not keep a list of which models those are -- the
    client sends the ordinary shape, reads the rejection and corrects itself.
    """
    from autef2.config import AutefConfig
    from autef2.llm import _QUIRKS, LLMClient, _learn_quirk

    model = "test-reasoning-model"
    _QUIRKS.pop(model, None)
    config = AutefConfig(model=model, reasoning_effort="medium", api_key="x")
    client = LLMClient.__new__(LLMClient)  # no network, no transport needed
    client.config = config

    first = client._request_kwargs([], 0.0, 512, json_mode=True)
    assert first["max_tokens"] == 512 and first["temperature"] == 0.0

    assert _learn_quirk(model, Exception(
        "Unsupported parameter: 'max_tokens' is not supported with this "
        "model. Use 'max_completion_tokens' instead."
    ))
    assert _learn_quirk(model, Exception(
        "Unsupported value: 'temperature' does not support 0.0 with this "
        "model. Only the default (1) is supported."
    ))

    after = client._request_kwargs([], 0.0, 512, json_mode=True)
    assert after["max_completion_tokens"] == 512
    assert "max_tokens" not in after and "temperature" not in after
    assert after["reasoning_effort"] == "medium"

    # An error we cannot answer must fall through to the ordinary retry path
    # rather than looping, and a rejection already learned is not new.
    assert not _learn_quirk(model, Exception("429 Rate limit reached"))
    assert not _learn_quirk(model, Exception(
        "Unsupported parameter: 'max_tokens' is not supported"
    ))
    _QUIRKS.pop(model, None)
