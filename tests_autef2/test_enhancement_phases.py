"""v1's other three capabilities, ported: generation, coverage, mutation.

These run the real phases against real pytest and real coverage; only the model
is scripted. What each group pins down is the thing v1 could not do:

* **generation** works on a project AUTEF was handed, not on
  ``source_files/<APP_NAME>``, and a generated file that is not usable is
  rejected instead of counted.
* **coverage** is measured in the project's own environment over its detected
  source roots, and the improvement is measured rather than assumed.
* **mutation** verifies that a test written to kill a mutant actually kills it.
  A test that passes either way inflates the score while testing nothing, and
  that is the failure mode with teeth.
"""

import sys
from pathlib import Path

import pytest

from autef2.chunker import module_import_name, split_module
# Aliased: pytest would otherwise collect `testable_modules` as a test function
# because the name starts with "test".
from autef2.chunker import testable_modules as list_testable_modules
from autef2.config import AutefConfig
from autef2.coverage_tool import CoverageTool, gaps
from autef2.enhance import (
    CoveragePhase,
    EnhanceOptions,
    GenerationPhase,
    MutationPhase,
)
from autef2.ingest import ingest
from autef2.llm import StubLLMClient
from autef2.models import Mutant
from autef2.mutation import MutationError, Mutator
from autef2.venv_manager import prepare_environment

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

CLASSIFY_SOURCE = '''
LIMIT = 100


def classify(n):
    if n < 0:
        return "negative"
    if n == 0:
        return "zero"
    if n > LIMIT:
        return "large"
    return "small"


def total(a, b):
    return a + b


def _private_helper(x):
    return x
'''


def make_config(tmp_path, **kwargs) -> AutefConfig:
    defaults = dict(
        workspace=tmp_path / "ws", use_venv=False, api_key="stub", max_attempts=2
    )
    defaults.update(kwargs)
    return AutefConfig(**defaults)


def _project(tmp_path, *, with_tests: str = "") -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    (root / "ops.py").write_text(CLASSIFY_SOURCE, encoding="utf-8")
    if with_tests:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / "test_ops.py").write_text(with_tests, encoding="utf-8")
    return root


def _ready(tmp_path, **kwargs):
    """Ingest, prepare an environment, and hand back both."""
    config = make_config(tmp_path)
    layout = ingest(str(_project(tmp_path, **kwargs)), config)
    return config, layout, prepare_environment(layout, config)


def scripted(config, reply):
    return StubLLMClient(config, responder=lambda messages: reply)


# ---------------------------------------------------------------------------
# chunking: what replaced the 512-character split
# ---------------------------------------------------------------------------


def test_units_are_whole_functions_and_classes(tmp_path):
    config, layout, _env = _ready(tmp_path)
    module = split_module(Path(layout.root) / "ops.py", layout)

    assert module.import_name == "ops"
    assert [u.name for u in module.units] == ["classify", "total"]
    # Private helpers are not part of the module's contract.
    assert "_private_helper" not in [u.name for u in module.units]
    # Whole, not sliced: the last line of the function is present.
    classify = next(u for u in module.units if u.name == "classify")
    assert 'return "small"' in classify.source
    # The header carries what the units assume exists.
    assert "LIMIT = 100" in module.header


def test_class_units_list_their_methods(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text(
        "class Thing:\n"
        "    def __init__(self, x):\n        self.x = x\n"
        "    def double(self):\n        return self.x * 2\n"
        "    def _hidden(self):\n        return 1\n",
        encoding="utf-8",
    )
    config = make_config(tmp_path)
    layout = ingest(str(root), config)

    module = split_module(Path(layout.root) / "m.py", layout)
    thing = module.units[0]

    assert thing.kind == "class"
    assert thing.members == ["__init__", "double"]
    assert "_hidden" not in thing.members


def test_import_name_follows_the_layout(tmp_path):
    root = tmp_path / "p"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "deep.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    config = make_config(tmp_path)
    layout = ingest(str(root), config)

    name = module_import_name(Path(layout.root) / "src" / "pkg" / "deep.py", layout)

    assert name == "pkg.deep", "src/ is an import root, so the package name starts at pkg"


def test_a_project_with_no_tests_still_has_testable_modules(tmp_path):
    """Regression: a flat project's test root is the project root, which once
    made every module look like test code and hid it from generation."""
    config, layout, _env = _ready(tmp_path)

    assert layout.test_files == []
    assert [Path(m.path).name for m in list_testable_modules(layout)] == ["ops.py"]


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


GOOD_TESTS = '''
from ops import classify, total


def test_negative():
    assert classify(-1) == "negative"


def test_small():
    assert classify(5) == "small"


def test_total():
    assert total(2, 3) == 5
'''


def test_generation_writes_a_working_suite_for_a_project_with_none(tmp_path):
    config, layout, env = _ready(tmp_path)

    outcome = GenerationPhase(layout, env, config, scripted(config, GOOD_TESTS)).run()

    assert len(outcome.accepted) == 1
    record = outcome.accepted[0]
    assert Path(record.test_file).name == "test_ops.py"
    assert (record.tests_collected, record.tests_passing) == (3, 3)
    # The layout handed back must include the new file, or later stages miss it.
    assert outcome.layout is not None
    assert [Path(f).name for f in outcome.layout.test_files] == ["test_ops.py"]


EXISTING_TESTS = '''
from ops import classify, total


def test_existing_passes():
    assert classify(-1) == "negative"


def test_existing_fails():
    assert total(2, 3) == 99
'''


def test_generation_never_overwrites_the_projects_own_tests(tmp_path):
    """The project's suite must survive generation.

    Generation runs for modules that already have tests once no untested module
    is left, and its preferred name -- ``test_<module>.py`` -- is exactly what
    that file is called. Writing over it destroyed the project's tests, and
    because the replacements were written against observed behaviour they all
    passed: the run then reported a suite with nothing failing, which reads as
    success. Deleting the user's tests must never be able to look like a win.
    """
    config, layout, env = _ready(tmp_path, with_tests=EXISTING_TESTS)
    own = Path(layout.root) / "tests" / "test_ops.py"
    original = own.read_text(encoding="utf-8")

    outcome = GenerationPhase(layout, env, config, scripted(config, GOOD_TESTS)).run()

    assert len(outcome.accepted) == 1
    written = Path(outcome.accepted[0].test_file)
    assert written.name != "test_ops.py", "the project's own file was targeted"
    assert own.read_text(encoding="utf-8") == original, "the project's tests changed"

    # And the failing test the project shipped is still there to be repaired.
    assert outcome.layout is not None
    assert sorted(Path(f).name for f in outcome.layout.test_files) == sorted(
        ["test_ops.py", written.name]
    )


def test_generation_replaces_its_own_earlier_output(tmp_path):
    """Re-running a phase should replace its own file, not accumulate copies."""
    config, layout, env = _ready(tmp_path)

    first = GenerationPhase(layout, env, config, scripted(config, GOOD_TESTS)).run()
    assert len(first.accepted) == 1
    second = GenerationPhase(
        first.layout or layout, env, config, scripted(config, GOOD_TESTS)
    ).run()

    assert len(second.accepted) == 1
    assert second.accepted[0].test_file == first.accepted[0].test_file
    assert len(list(Path(layout.root).rglob("test_*.py"))) == 1


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("def test_x(:\n    pass\n", "does not parse"),
        ("from ops import classify\n\nx = 1\n", "no test functions"),
        ("def test_x():\n    assert True\n", "does not import ops"),
        ("", "returned nothing"),
    ],
)
def test_unusable_generated_code_is_rejected_not_written(tmp_path, reply, expected):
    """v1 wrote whatever came back, so a file that could not even be imported
    still counted as tests generated."""
    config, layout, env = _ready(tmp_path)

    outcome = GenerationPhase(layout, env, config, scripted(config, reply)).run()

    assert outcome.accepted == []
    assert expected in (outcome.records[0].error or "")
    assert not list(Path(layout.root).rglob("test_*.py")), "nothing should be written"


def test_generated_tests_that_fail_are_kept_for_the_repair_loop(tmp_path):
    """A failing generated test is the repair loop's input, not a rejection."""
    config, layout, env = _ready(tmp_path)
    wrong = (
        "from ops import total\n\n\ndef test_total():\n    assert total(2, 3) == 6\n"
    )

    outcome = GenerationPhase(layout, env, config, scripted(config, wrong)).run()

    assert len(outcome.accepted) == 1
    record = outcome.accepted[0]
    assert record.tests_collected == 1 and record.tests_passing == 0
    assert Path(record.test_file).is_file()


def test_generation_composes_with_repair(tmp_path):
    """The whole point of keeping both: generate, then fix what fails."""
    from autef2.pipeline import run_project

    config = make_config(tmp_path)
    root = _project(tmp_path)
    wrong = (
        "from ops import total\n\n\ndef test_total():\n    assert total(2, 3) == 6\n"
    )
    fixed = (
        "def test_total():\n    assert total(2, 3) == 5\n"
    )

    def responder(messages):
        text = " ".join(m["content"] for m in messages)
        if "test-failure analyst" in messages[0]["content"]:
            import json

            return json.dumps(
                {
                    "root_cause": "assertion_mismatch",
                    "at_fault": "test",
                    "confidence": 0.9,
                    "explanation": "scripted",
                    "evidence": [],
                }
            )
        if "Write a unit test suite" in text:
            return wrong
        return fixed

    report = run_project(
        str(root),
        config,
        llm=StubLLMClient(config, responder=responder),
        enhance=EnhanceOptions(generate=True),
    )

    assert len(report.generated) == 1 and report.generated[0].accepted
    assert report.records, "the failing generated test should have been repaired"
    assert any(r.fixed for r in report.records)
    assert report.after is not None and report.after.failures == []


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


PARTIAL_TESTS = 'from ops import classify\n\n\ndef test_small():\n    assert classify(5) == "small"\n'


def test_coverage_is_measured_in_the_project_environment(tmp_path):
    config, layout, env = _ready(tmp_path, with_tests=PARTIAL_TESTS)

    snapshot = CoverageTool(layout, env, config).measure()

    assert snapshot.measured, snapshot.error
    assert 0 < snapshot.line_rate < 1, "this suite covers some but not all lines"
    assert snapshot.branches > 0, "branch coverage must be on"

    gap = gaps(snapshot)[0]
    assert Path(gap.path).name == "ops.py"
    assert gap.missing_lines, "the unexecuted lines must be identified"
    assert gap.missing_branches, "the untaken branches must be identified"


def test_coverage_survives_a_project_that_configures_parallel_mode(tmp_path):
    """The project's own coverage config is the one in force, and plenty of
    real projects set ``parallel = true`` so a CI matrix can merge runs.

    Coverage then ignores the data-file name it was given and writes
    ``<name>.<host>.<pid>.<random>`` instead. Checking for the requested name
    finds nothing and concludes "the coverage run produced no data" -- about a
    run that measured the entire suite. structlog is a real instance.
    """
    config, layout, env = _ready(tmp_path, with_tests=PARTIAL_TESTS)
    (Path(layout.root) / "pyproject.toml").write_text(
        "[tool.coverage.run]\nparallel = true\n", encoding="utf-8"
    )

    snapshot = CoverageTool(layout, env, config).measure()

    assert snapshot.measured, snapshot.error
    assert snapshot.statements > 0
    assert 0 < snapshot.line_rate < 1


def test_coverage_phase_raises_coverage_and_measures_the_gain(tmp_path):
    config, layout, env = _ready(tmp_path, with_tests=PARTIAL_TESTS)
    filling = (
        "from ops import classify, total\n\n\n"
        'def test_negative():\n    assert classify(-1) == "negative"\n\n\n'
        'def test_zero():\n    assert classify(0) == "zero"\n\n\n'
        'def test_large():\n    assert classify(500) == "large"\n\n\n'
        "def test_total():\n    assert total(1, 2) == 3\n"
    )

    outcome = CoveragePhase(layout, env, config, scripted(config, filling)).run()

    assert outcome.before.measured and outcome.after.measured
    assert outcome.line_gain > 0, "coverage should have risen"
    assert outcome.branch_gain > 0
    assert len(outcome.accepted) == 1
    assert Path(outcome.accepted[0].test_file).name == "test_ops_coverage.py"


def test_coverage_tests_go_to_their_own_file(tmp_path):
    """The project's own test file is not edited: a merge failure there would
    break tests the user wrote, for no gain a separate file does not give."""
    config, layout, env = _ready(tmp_path, with_tests=PARTIAL_TESTS)
    original = (Path(layout.root) / "tests" / "test_ops.py").read_text(encoding="utf-8")

    CoveragePhase(layout, env, config, scripted(config, PARTIAL_TESTS)).run()

    assert (Path(layout.root) / "tests" / "test_ops.py").read_text(
        encoding="utf-8"
    ) == original


# ---------------------------------------------------------------------------
# mutation
# ---------------------------------------------------------------------------


def test_mutation_sites_cover_the_operator_families(tmp_path):
    config, layout, _env = _ready(tmp_path)

    operators = {s.operator for s in Mutator(layout).sites()}

    assert any("comparison" in o for o in operators)
    assert any("arithmetic" in o for o in operators)
    assert any("literal" in o for o in operators)


def test_a_mutation_changes_one_operator_and_restores_exactly(tmp_path):
    config, layout, _env = _ready(tmp_path)
    mutator = Mutator(layout)
    site = next(s for s in mutator.sites() if s.operator == "arithmetic + -> -")
    path = Path(site.file)
    before = path.read_text(encoding="utf-8")

    original = mutator.apply(site)
    mutated = path.read_text(encoding="utf-8")

    assert "return a - b" in mutated
    assert mutated != before
    # Exactly one line differs.
    assert sum(
        1
        for old, new in zip(before.splitlines(), mutated.splitlines())
        if old != new
    ) == 1

    mutator.restore(site, original)
    assert path.read_text(encoding="utf-8") == before


def test_a_mutation_that_would_not_parse_is_refused(tmp_path):
    config, layout, _env = _ready(tmp_path)
    mutator = Mutator(layout)
    site = next(iter(mutator.sites()))
    site.original_text = "definitely not there"

    with pytest.raises(MutationError):
        mutator.apply(site)


def test_a_weak_suite_lets_mutants_survive(tmp_path):
    """The measurement that makes the phase worth running."""
    weak = (
        "from ops import classify, total\n\n\n"
        "def test_runs():\n    assert classify(5) is not None\n\n\n"
        "def test_total_runs():\n    assert total(2, 3) is not None\n"
    )
    config, layout, env = _ready(tmp_path, with_tests=weak)

    snapshot = MutationPhase(layout, env, config, None).score(max_mutants=8)

    assert snapshot.measured
    assert snapshot.survived > 0, "a truthiness-only suite must miss mutants"
    assert snapshot.score < 1.0


def test_mutation_refuses_to_score_a_failing_suite(tmp_path):
    """A score against a failing suite cannot distinguish 'caught it' from
    'was already broken'."""
    broken = 'from ops import classify\n\n\ndef test_wrong():\n    assert classify(5) == "enormous"\n'
    config, layout, env = _ready(tmp_path, with_tests=broken)

    outcome = MutationPhase(layout, env, config, None).run(max_mutants=4)

    assert outcome.skipped_reason is not None
    assert "already fail" in outcome.skipped_reason
    assert outcome.before is None


#: One function, one mutable operator, one weak test. Keeping it this small
#: means every surviving mutant is one the scripted killer test could address,
#: so the assertions are about the verification and not about which mutant the
#: sampler happened to pick.
ONE_OPERATOR_SOURCE = "def total(a, b):\n    return a + b\n"
ONE_OPERATOR_WEAK_TEST = (
    "from calc import total\n\n\n"
    "def test_total_runs():\n    assert total(2, 3) is not None\n"
)


def _one_operator_project(tmp_path):
    root = tmp_path / "single"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(ONE_OPERATOR_SOURCE, encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        ONE_OPERATOR_WEAK_TEST, encoding="utf-8"
    )
    config = make_config(tmp_path)
    layout = ingest(str(root), config)
    return config, layout, prepare_environment(layout, config)


def test_a_verified_killer_test_raises_the_score(tmp_path):
    """v1 never checked whether its new test killed anything."""
    config, layout, env = _one_operator_project(tmp_path)
    killer = "from calc import total\n\n\ndef test_exact():\n    assert total(2, 3) == 5\n"

    outcome = MutationPhase(layout, env, config, scripted(config, killer)).run(
        max_mutants=4, max_survivors=2
    )

    assert outcome.newly_killed >= 1, [r.error for r in outcome.records]
    assert outcome.score_gain > 0
    # The baseline must not be rewritten to match the improvement.
    assert outcome.before.score < outcome.after.score
    assert any(m.killed_after_generation for m in outcome.after.mutants)


def test_a_test_that_does_not_detect_its_mutant_is_discarded(tmp_path):
    """It would raise the reported score without raising the suite's
    sensitivity, which is the failure mode with teeth."""
    config, layout, env = _one_operator_project(tmp_path)
    useless = (
        "from calc import total\n\n\n"
        "def test_truthy():\n    assert total(2, 3) is not None\n"
    )

    outcome = MutationPhase(layout, env, config, scripted(config, useless)).run(
        max_mutants=4, max_survivors=2
    )

    assert outcome.newly_killed == 0
    assert outcome.score_gain == 0
    assert any(
        "does not detect it" in (r.error or "") for r in outcome.records
    ), [r.error for r in outcome.records]
    assert not list(
        Path(layout.root).rglob("test_*_mutation.py")
    ), "a rejected killer test must be removed from the project"


def test_a_killer_test_that_fails_on_clean_source_is_discarded(tmp_path):
    """Fails on the mutant AND passes without it. A test that fails on both is
    broken, not a kill."""
    config, layout, env = _one_operator_project(tmp_path)
    always_fails = (
        "from calc import total\n\n\ndef test_broken():\n    assert total(2, 3) == 99\n"
    )

    outcome = MutationPhase(layout, env, config, scripted(config, always_fails)).run(
        max_mutants=4, max_survivors=1
    )

    assert outcome.newly_killed == 0
    assert any(
        "does not pass on the unmutated source" in (r.error or "")
        for r in outcome.records
    ), [r.error for r in outcome.records]


def test_a_package_with_no_tests_is_not_invisible_to_generation(tmp_path):
    """Two ways a package could vanish from generation and mutation, silently.

    First, ``__init__.py`` was skipped by filename -- so python-tabulate, which
    keeps its whole implementation there, reported "0 modules considered" and
    had its mutation score computed over ``__main__.py`` and ``cli.py`` instead
    of the library. A shim ``__init__.py`` still yields no units, so unit
    extraction decides rather than the filename.

    Second, a project with no tests has ``test_roots == [project root]``. For a
    package layout that root is the *parent* of the source root, and excluding
    it as "test code" excluded the entire package. verigak/progress looked like
    a project with nothing testable in it for exactly this reason; it has five
    testable modules and ninety-nine mutation sites.

    An empty plan is not an error, so both failed quietly.
    """
    from autef2.chunker import _source_files
    from autef2.ingest import analyse
    from autef2.mutation import Mutator

    root = tmp_path / "pkg"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "__init__.py").write_text(CLASSIFY_SOURCE, encoding="utf-8")
    (root / "lib" / "shim").mkdir()
    (root / "lib" / "shim" / "__init__.py").write_text(
        "from lib import classify  # re-export only\n", encoding="utf-8"
    )
    layout = analyse(root)

    names = {p.name for p in _source_files(layout)}
    assert "__init__.py" in names, "the implementation module was skipped"

    testable = {m.import_name for m in list_testable_modules(layout, limit=10)}
    assert any(t.endswith("lib") or t == "lib" for t in testable), testable
    assert not any("shim" in t for t in testable), "a re-export shim is not testable"

    sites = Mutator(layout).sites(limit=50)
    assert sites, "nothing to mutate means no mutation score for the library"


def test_every_mutant_is_reported_with_what_happened_to_it(tmp_path):
    """A mutation score is not checkable without the mutants behind it.

    "One survived" invites "which one, and why", and the answer -- the file,
    the line, the operator, the change itself, and whether a killer test was
    attempted and rejected -- is what turns a number into a finding. This also
    pins the pairing: the phase writes one killer test per survivor in order,
    so an attempt must attach to the mutant it was written for and to no other.
    """
    from autef2.web.server import _mutants

    config, layout, env = _one_operator_project(tmp_path)

    # A test that cannot detect anything: the rejection path.
    useless = "from calc import total\n\n\ndef test_useless():\n    assert total\n"
    outcome = MutationPhase(layout, env, config, scripted(config, useless)).run(
        max_mutants=6, max_survivors=1, seed=7
    )
    assert outcome.before is not None and outcome.before.measured, outcome.skipped_reason

    rows = _mutants(outcome)
    assert len(rows) == len(outcome.before.mutants)
    for row in rows:
        assert row["file"] and row["line"] and row["operator"]
        assert row["original"] and row["mutated"]
        assert row["original"] != row["mutated"], "a mutant must change something"

    attempted = [r for r in rows if r["attempted"]]
    assert len(attempted) <= 1, "max_survivors=1 means at most one attempt"
    if attempted:
        assert attempted[0]["attempt_error"], "a rejected attempt must say why"
        assert not attempted[0]["killed"]

    # The reason belongs to the one mutant it was written for, not to every
    # mutant in the same file.
    assert all(r["attempt_error"] is None for r in rows if not r["attempted"])


def test_a_killer_test_that_works_is_marked_against_its_own_mutant(tmp_path):
    from autef2.web.server import _mutants

    config, layout, env = _one_operator_project(tmp_path)
    killer = "from calc import total\n\n\ndef test_exact():\n    assert total(2, 3) == 5\n"
    outcome = MutationPhase(layout, env, config, scripted(config, killer)).run(
        max_mutants=6, max_survivors=2, seed=7
    )
    rows = _mutants(outcome)
    newly = [r for r in rows if r["killed_after_generation"]]
    for row in newly:
        assert row["killed"], "marked as newly killed, so it must read as killed"
        assert row["attempt_error"] is None
