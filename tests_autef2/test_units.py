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


def test_budget_grows_when_reasoning_eats_it():
    """A reasoning model that thinks past its budget is retried, not believed.

    ``max_completion_tokens`` covers thinking and writing together, so a budget
    sized for the answer alone can be spent before a single visible token is
    emitted. The old code read that empty content as "the model returned
    nothing" and rejected the test it was asking for.
    """
    from autef2.config import AutefConfig
    from autef2.llm import _MIN_BUDGET, _QUIRKS, LLMClient

    model = "test-budget-model"
    _QUIRKS.pop(model, None)
    _MIN_BUDGET.pop(model, None)

    class _Choice:
        def __init__(self, content, finish):
            self.message = type("M", (), {"content": content})()
            self.finish_reason = finish

    class _Response:
        def __init__(self, content, finish):
            self.choices = [_Choice(content, finish)]
            self.usage = None

    calls = []

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs["max_completion_tokens"])
            # Starved at the first budget, fine once it is grown.
            if kwargs["max_completion_tokens"] < 4096:
                return _Response("", "length")
            return _Response("def test_ok(): pass", "stop")

    client = LLMClient.__new__(LLMClient)
    client.config = AutefConfig(model=model, api_key="x", max_output_tokens=1024)
    client.usage = __import__("autef2.llm", fromlist=["Usage"]).Usage()
    client._parent = None
    client._client = type("C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})()
    _QUIRKS[model] = {"max_completion_tokens"}

    assert client.complete([{"role": "user", "content": "hi"}]) == "def test_ok(): pass"
    assert calls == [1024, 4096], "the budget should grow once, not thrash"
    assert _MIN_BUDGET[model] == 4096, "the lesson must stick for later calls"

    # A later call starts at the learned budget rather than re-learning it.
    calls.clear()
    client.complete([{"role": "user", "content": "hi"}])
    assert calls == [4096]

    _QUIRKS.pop(model, None)
    _MIN_BUDGET.pop(model, None)


def _failure(message="", longrepr="", exc_type="AssertionError", phase="call"):
    return TestFailure(
        nodeid="tests/test_x.py::test_y",
        outcome=Outcome.FAILED,
        exception_type=exc_type,
        exception_message=message,
        longrepr=longrepr,
        phase=phase,
    )


def test_classification_reads_the_error_not_the_test_source():
    """pytest's longrepr carries the test's own source alongside the error.

    A passing ``assert_called_once()`` three lines above the real failure used
    to classify the whole test as a mock problem at 0.9 confidence, on the
    strength of a line that worked.
    """
    longrepr = (
        "    def test_total():\n"
        "        mock.assert_called_once()\n"
        "        assert total(2, 3) == 6\n"
        "E       assert 5 == 6\n"
    )
    cause, confidence = heuristic_classify(
        _failure("assert 5 == 6", longrepr)
    )

    assert cause is RootCause.ASSERTION_MISMATCH, (
        "a passing mock assertion in the source decided the diagnosis"
    )

    # The same signature in the error itself still classifies as a mock problem.
    cause, _ = heuristic_classify(
        _failure("Expected 'send' to have been called once.",
                 "E       AssertionError: Expected 'send' to have been called once.")
    )
    assert cause is RootCause.MOCK_MISCONFIGURATION


def test_service_names_are_matched_as_errors_not_as_words():
    """ENVIRONMENT_DEPENDENCY is NON_REPAIRABLE, so a loose match skips a test
    permanently without one repair attempt."""
    # A test that merely mentions Docker is not an environment failure.
    cause, _ = heuristic_classify(_failure(
        "assert 1 == 2",
        "    def test_docker_image_name_is_built():\n"
        "        assert build_name() == 'redis:7'\n"
        "E       assert 'redis:6' == 'redis:7'\n",
    ))
    assert cause is RootCause.ASSERTION_MISMATCH, (
        "a test whose subject is Docker was called an environment dependency"
    )

    # A real one still is.
    cause, _ = heuristic_classify(_failure(
        "Error 111 connecting to localhost:6379. Connection refused.",
        "E       redis.exceptions.ConnectionError: Error 111 connecting",
        exc_type="ConnectionError",
    ))
    assert cause is RootCause.ENVIRONMENT_DEPENDENCY


def test_source_defect_sentinel_must_stand_alone():
    """Mentioning the sentinel is not claiming it."""
    from autef2.agents.autofix import _claims_source_defect

    assert _claims_source_defect("NO_TEST_FIX_NEEDED")
    assert _claims_source_defect("some reasoning\n  NO_TEST_FIX_NEEDED  \nmore")
    assert _claims_source_defect("# NO_TEST_FIX_NEEDED")
    assert not _claims_source_defect(
        "This is not a NO_TEST_FIX_NEEDED case; the test is simply wrong."
    )
    assert not _claims_source_defect(
        "def test_x():\n    # unlike NO_TEST_FIX_NEEDED situations, fix this\n    pass"
    )


def test_an_unsure_non_repairable_verdict_does_not_skip_the_test():
    """A permanent skip on a 0.3-confidence guess discards a repairable test.

    The skip ends the attempt before the ladder runs, and quarantine then takes
    the test out of the suite -- so an unsure guess is acted on as though it
    were certain. The floor sends anything below it down the ladder instead.
    """
    from autef2.models import Diagnosis
    from autef2.orchestrator import may_skip

    def diagnosis(confidence):
        return Diagnosis(
            root_cause=RootCause.PRODUCTION_BUG,
            at_fault="source",
            confidence=confidence,
            explanation="the source looks wrong",
        )

    assert may_skip(diagnosis(0.9), 0.6)
    assert may_skip(diagnosis(0.6), 0.6), "the floor is inclusive"
    assert not may_skip(diagnosis(0.3), 0.6)
    assert not may_skip(diagnosis(0.0), 0.6)


def test_a_package_directory_never_goes_on_the_import_path(tmp_path):
    """Putting ``pyparsing/`` on sys.path makes its modules shadow the stdlib.

    ``import pyparsing`` already works from the project root. Adding the
    package directory itself only makes every module inside it importable as a
    top-level name, so ``pyparsing/warnings.py`` shadows ``warnings`` and the
    whole suite dies during collection. Measured on the real repository: 0
    tests collected before this rule, 4155 after.
    """
    from autef2.ingest import _detect_import_roots

    root = tmp_path / "proj"
    package = root / "thing"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "warnings.py").write_text("", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()

    roots = _detect_import_roots(root, [package], [tests], [])

    assert package not in roots, "the package itself shadows the stdlib"
    assert root in roots, "the parent must be there for `import thing` to work"


def test_a_src_directory_still_goes_on_the_import_path(tmp_path):
    """The rule is about packages, not about every source root."""
    from autef2.ingest import _detect_import_roots

    root = tmp_path / "proj"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "thing.py").write_text("", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()

    roots = _detect_import_roots(root, [src], [tests], [])

    assert src in roots, "a src/ root is not a package and must stay"


def test_generation_shows_the_model_how_the_project_writes_tests(tmp_path):
    """Nothing in stages 4-6 used to show the model one existing test.

    Asked to write tests for an unfamiliar codebase with no sight of its
    conventions, the model guesses them -- and tests written blind to the house
    style are the ones that fail on arrival.
    """
    from autef2.agents.generation import build_generation_prompt
    from autef2.chunker import split_module
    from autef2.models import ProjectLayout

    root = tmp_path / "proj"
    tests = root / "tests"
    tests.mkdir(parents=True)
    module = root / "ops.py"
    module.write_text("def total(a, b):\n    return a + b\n", encoding="utf-8")
    (tests / "test_existing.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('a,b', [(1, 2)])\n"
        "def test_house_style(a, b, sample_widget):\n    assert a < b\n",
        encoding="utf-8",
    )
    (tests / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef sample_widget():\n    return 1\n",
        encoding="utf-8",
    )
    layout = ProjectLayout(
        name="proj", root=str(root), import_roots=[str(root)],
        test_roots=[str(tests)],
    )

    prompt = build_generation_prompt(split_module(module, layout), layout)

    assert "test_house_style" in prompt, "the project's own tests were not shown"
    assert "parametrize" in prompt, "its conventions were not visible"
    assert "sample_widget" in prompt, "existing fixtures were not offered"
    assert "do not repeat or import it" in prompt


def test_a_multi_project_comparison_pools_every_project():
    """Taking reports[0] silently reduced a four-project run to one.

    The exact McNemar test needs at least six discordant pairs all favouring v2
    before p < 0.05 is attainable, which one small repository cannot supply --
    so the significance test could never fire however many projects the
    benchmark was given.
    """
    from autef2.eval.compare import _merge_reports
    from autef2.models import RepairRecord, RunReport

    def report(project, fixed_count, total):
        r = RunReport(project=project)
        r.records = [
            RepairRecord(nodeid=f"tests/test_a.py::test_{i}", signature=str(i),
                         fixed=i < fixed_count)
            for i in range(total)
        ]
        r.llm_calls, r.cost_usd = total, 0.5
        return r

    merged = _merge_reports([
        report("schema", 2, 3), report("cachetools", 3, 4), report("sqlparse", 1, 5),
    ])

    assert len(merged.records) == 12, "only one project's records were kept"
    assert sum(1 for r in merged.records if r.fixed) == 6
    assert merged.llm_calls == 12 and merged.cost_usd == 1.5
    # Two projects' test_0 must stay distinct observations.
    ids = [r.nodeid for r in merged.records]
    assert len(set(ids)) == len(ids), "node ids collided across projects"
    assert all("::" in i for i in ids)


def test_a_commit_is_fetched_as_a_commit_not_as_a_branch():
    """A pinned revision 404s if it is looked for under refs/heads."""
    from autef2.ingest import _looks_like_sha

    assert _looks_like_sha("961dcff3f42e73b245aef65e377fe82763b257bb")
    assert _looks_like_sha("961dcff")
    assert not _looks_like_sha("main")
    assert not _looks_like_sha("master")
    assert not _looks_like_sha("release/2.0")
    assert not _looks_like_sha("abc"), "too short to be an abbreviation"


def test_the_benchmark_manifest_pins_every_project():
    """A manifest on a moving branch cannot reproduce a quoted number."""
    from autef2.eval.benchmark import load_specs

    specs = load_specs(Path(__file__).resolve().parents[1] / "benchmarks" / "manifest.json")

    assert len(specs) >= 3, "the portability claim needs more than two projects"
    unpinned = [s.name for s in specs if not s.revision]
    assert not unpinned, f"unpinned projects: {unpinned}"
    for spec in specs:
        assert spec.revision in spec.pinned_source
    # The sample has to actually vary, or it is one project measured four times.
    assert len({s.stratum for s in specs}) == len(specs)


def test_the_test_subprocess_does_not_inherit_credentials():
    """The suite being run is arbitrary code from an uploaded repository.

    A conftest that reads os.environ is entirely ordinary, and it used to be
    handed the operator's whole environment, OPENAI_API_KEY included.
    """
    from autef2.runner import _without_secrets

    source = {
        "PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C.UTF-8",
        "PYTHONPATH": "/src",
        "OPENAI_API_KEY": "sk-live", "AWS_SECRET_ACCESS_KEY": "aws",
        "MY_SERVICE_TOKEN": "t", "DB_PASSWORD": "p", "GITHUB_TOKEN": "gh",
        "AUTEF_PASSWORD": "autef2025",
    }

    env = _without_secrets(source)

    assert set(env) == {"PATH", "HOME", "LANG", "PYTHONPATH"}
    assert not any("sk-live" in v for v in env.values())


def test_the_enhancement_table_reports_one_arm_and_says_why():
    """v1 has no coverage or mutation stage, so a zeros column would lie.

    A table showing "v1: 0%, v2: 90%" reads as a score of nil rather than as
    the absence of the capability, which is a different and much stronger
    claim than the evidence supports.
    """
    from autef2.eval.metrics import _enhancement_section
    from autef2.models import CoverageSnapshot, Mutant, MutationSnapshot, RunReport

    report = RunReport(project="cachetools")
    report.coverage_before = CoverageSnapshot(
        measured=True, statements=100, covered_statements=80,
        branches=20, covered_branches=16)
    report.coverage_after = CoverageSnapshot(
        measured=True, statements=100, covered_statements=100,
        branches=20, covered_branches=20)
    report.mutation_before = MutationSnapshot(measured=True, mutants=[
        Mutant(file="a.py", lineno=1, operator="==", original="a", mutated="b",
               killed=True),
        Mutant(file="a.py", lineno=2, operator="==", original="a", mutated="b",
               killed=False),
        Mutant(file="a.py", lineno=3, operator="==", original="a", mutated="b",
               error="not scored: budget"),
    ])
    report.mutation_after = report.mutation_before

    lines = _enhancement_section({"autef2": [report], "baseline": []})
    text = "\n".join(lines)

    assert "cachetools" in text
    assert "80% -> 100%" in text, "coverage before/after is not shown"
    assert "no mutation testing" in text, "the absence is not explained"
    # Killed over scored, and the unscored mutant reported separately.
    assert "1/2" in text and "| 1 |" in text


def test_no_enhancement_table_when_nothing_was_measured():
    from autef2.eval.metrics import _enhancement_section
    from autef2.models import RunReport

    assert _enhancement_section({"autef2": [RunReport(project="x")]}) == []


def test_changing_the_model_rebuilds_the_client():
    """The dropdown was a lie after the first stage that used the model.

    The client was cached for the life of the session, so later stages went on
    calling -- and billing -- the original model while the page showed the new
    one. Anyone comparing two models from the UI got one model twice.
    """
    from autef2.web.server import Session, _config, _llm

    session = Session("demo")
    session.settings["model"] = "gpt-4o-mini"
    first = _llm(session, _config(session))
    first.usage.add(10, 5, 0.01)

    again = _llm(session, _config(session))
    assert again is first, "an unchanged setting must not rebuild the client"

    session.settings["model"] = "gpt-5.6-sol"
    second = _llm(session, _config(session))

    assert second is not first, "the model changed and the client did not"
    assert second.config.model == "gpt-5.6-sol"
    # The tally belongs to the session, not to one model.
    assert second.usage.prompt_tokens == 10 and second.usage.cost_usd == 0.01

    session.settings["reasoning_effort"] = "high"
    third = _llm(session, _config(session))
    assert third is not second, "effort changed and the client did not"


def test_an_unpriced_model_warns_once_and_costs_at_the_dearest_rate(caplog):
    """A missing price entry used to bill at the cheapest rate in the table.

    Behind a debug line nobody would see, which is how a cost-per-fix figure
    ends up an order of magnitude out.
    """
    import logging

    from autef2.config import MODEL_PRICING
    from autef2.llm import _PRICING_WARNED

    _PRICING_WARNED.discard("some-unreleased-model")
    dearest = max(MODEL_PRICING.values(), key=lambda p: p["output"])
    cheapest = min(MODEL_PRICING.values(), key=lambda p: p["output"])
    assert dearest["output"] > cheapest["output"], "the table must have a spread"

    class _Usage:
        prompt_tokens = 1000
        completion_tokens = 1000

    class _Response:
        usage = _Usage()

    from autef2.config import AutefConfig
    from autef2.llm import LLMClient, Usage

    client = LLMClient.__new__(LLMClient)
    client.config = AutefConfig(model="some-unreleased-model", api_key="x")
    client.usage = Usage()
    client._parent = None

    with caplog.at_level(logging.WARNING):
        client._record(_Response())

    assert "No pricing is configured" in caplog.text
    assert client.usage.cost_usd == 1000 * dearest["input"] + 1000 * dearest["output"]
    _PRICING_WARNED.discard("some-unreleased-model")
