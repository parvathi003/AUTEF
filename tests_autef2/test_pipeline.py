"""End-to-end tests of the repair loop, with a scripted model.

These run the real pipeline -- ingest, pytest, resolve, diagnose, patch, re-run
-- and only the model is substituted. That is deliberate: the behaviour worth
testing is the loop's, and a scripted model lets us drive it into the cases
that matter (a fix that works, a fix that has to escalate, a fix that cheats).
"""

import json
import shutil

import pytest

from autef2.config import AutefConfig
from autef2.llm import StubLLMClient
from autef2.pipeline import run_project

from conftest import SAMPLE_PROJECT

# Correct repairs for the two deliberately broken tests in the fixture.
GOOD_MULTIPLY = """
def test_multiply_returns_product(self):
    self.assertEqual(self.calc.multiply(3, 4), 12)
"""

GOOD_DESCRIBE = """
def test_describe_uses_precision(self):
    self.assertEqual(self.calc.describe(), "Calculator(precision=2)")
"""

# A "fix" that passes by deleting the assertion.
CHEATING_MULTIPLY = """
def test_multiply_returns_product(self):
    self.calc.multiply(3, 4)
"""

# A fix that parses but does not work.
USELESS_MULTIPLY = """
def test_multiply_returns_product(self):
    self.assertEqual(self.calc.multiply(3, 4), 13)
"""


def _is_diagnosis(messages) -> bool:
    return "test-failure analyst" in messages[0]["content"]


def _diagnosis_for(text: str) -> str:
    cause = "api_misuse" if "AttributeError" in text else "assertion_mismatch"
    return json.dumps(
        {
            "root_cause": cause,
            "at_fault": "test",
            "confidence": 0.9,
            "explanation": "scripted",
            "evidence": [],
        }
    )


def make_config(tmp_path) -> AutefConfig:
    return AutefConfig(
        workspace=tmp_path / "ws",
        use_venv=False,
        use_signature_cache=False,
        api_key="stub",
        max_attempts=3,
    )


def scripted(config, repairs):
    """A model that answers diagnoses truthfully and repairs from ``repairs``.

    ``repairs`` maps a test function name to a list of replies, consumed one
    per attempt, so escalation can be exercised.
    """
    pending = {name: list(replies) for name, replies in repairs.items()}

    def responder(messages):
        user = messages[-1]["content"]
        if _is_diagnosis(messages):
            return _diagnosis_for(user)
        for name, replies in pending.items():
            if name in user:
                return replies.pop(0) if replies else "NO_TEST_FIX_NEEDED"
        return "NO_TEST_FIX_NEEDED"

    return StubLLMClient(config, responder=responder)


# ---------------------------------------------------------------------------


def test_repairs_both_failures_and_verifies_them(tmp_path):
    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [GOOD_MULTIPLY],
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
    )

    report = run_project(SAMPLE_PROJECT, config, llm=llm)

    assert report.error is None
    assert len(report.before.failures) == 2
    assert len(report.records) == 2
    assert all(r.fixed for r in report.records), [
        (r.nodeid, [a.rejected_reason or a.new_failure for a in r.attempts])
        for r in report.records
    ]
    # Each was fixed at the first rung of its ladder.
    assert all(r.attempts_used == 1 for r in report.records)
    # And the suite really is green now -- this is the verification v1 skipped.
    assert report.after.failures == []
    assert len(report.after.passed) == 4


def test_rejects_a_fix_that_deletes_the_assertion(tmp_path):
    """The cheat that a fix-rate metric alone would happily reward."""
    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [CHEATING_MULTIPLY] * 3,
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
    )

    report = run_project(SAMPLE_PROJECT, config, llm=llm)

    multiply = next(
        r for r in report.records if "multiply" in r.nodeid
    )
    assert not multiply.fixed
    assert any(
        a.rejected_reason and "weakened" in a.rejected_reason
        for a in multiply.attempts
    ), [a.rejected_reason for a in multiply.attempts]

    # Rolled back: the original wrong expectation is still there.
    test_file = next(
        f for f in report.layout.test_files if "test_operations" in f
    )
    assert "14" in open(test_file, encoding="utf-8").read()


def test_escalates_to_a_different_strategy_after_a_failed_repair(tmp_path):
    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [USELESS_MULTIPLY, GOOD_MULTIPLY],
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
    )

    report = run_project(SAMPLE_PROJECT, config, llm=llm)

    multiply = next(r for r in report.records if "multiply" in r.nodeid)
    assert multiply.fixed
    assert multiply.attempts_used == 2

    strategies = [a.strategy_id for a in multiply.attempts]
    assert len(set(strategies)) == 2, "escalation must change strategy, not retry"
    assert multiply.attempts[0].new_failure, "first attempt should record why it failed"


def test_honours_a_reported_source_defect(tmp_path):
    """The model can say the test is right and the source is wrong."""
    config = make_config(tmp_path)
    llm = scripted(config, {})  # every repair replies NO_TEST_FIX_NEEDED

    report = run_project(SAMPLE_PROJECT, config, llm=llm)

    assert all(not r.fixed for r in report.records)
    assert all(
        r.skipped_reason and "source is at fault" in r.skipped_reason
        for r in report.records
    )
    # Nothing was written, so the suite is exactly as it was.
    assert len(report.after.failures) == 2


def test_dry_run_makes_no_model_calls(tmp_path):
    config = make_config(tmp_path)
    llm = scripted(config, {})

    report = run_project(SAMPLE_PROJECT, config, llm=llm, dry_run=True)

    assert report.arm == "dry-run"
    assert llm.calls == []
    assert len(report.before.failures) == 2
    # Resolution still happened, without spending anything.
    assert all(f.test_file for f in report.before.failures)


def test_max_tests_limits_the_work(tmp_path):
    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [GOOD_MULTIPLY],
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
    )

    report = run_project(SAMPLE_PROJECT, config, llm=llm, max_tests=1)
    assert len(report.records) == 1


def test_signature_cache_skips_rediagnosis(tmp_path):
    """A second identical failure reuses the strategy instead of re-diagnosing."""
    from autef2.cache import SignatureCache
    from autef2.ingest import analyse
    from autef2.models import RootCause

    cache = SignatureCache(tmp_path / "cache.json")
    cache.record_success("sig", RootCause.ASSERTION_MISMATCH, "align_expected_value")

    entry = cache.lookup("sig")
    assert entry is not None
    assert entry.strategy_id == "align_expected_value"
    assert cache.stats()["hits"] == 1


# ---------------------------------------------------------------------------
# baseline arm
# ---------------------------------------------------------------------------


def test_baseline_arm_applies_without_verifying(tmp_path):
    """v1's behaviour: one attempt, written straight to disk, never re-run."""
    from autef2.eval.baseline import BaselineOrchestrator
    from autef2.ingest import ingest
    from autef2.venv_manager import prepare_environment

    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [USELESS_MULTIPLY],
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
    )

    layout = ingest(SAMPLE_PROJECT, config)
    environment = prepare_environment(layout, config)
    report = BaselineOrchestrator(layout, environment, config, llm).run()

    assert len(report.records) == 2
    assert all(r.attempts_used == 1 for r in report.records), "one attempt, always"

    # The useless repair was still written -- the arm never checks. The harness
    # scores it from the post-run suite instead.
    multiply = next(r for r in report.records if "multiply" in r.nodeid)
    describe = next(r for r in report.records if "describe" in r.nodeid)
    assert multiply.attempts[0].applied
    assert not multiply.fixed
    assert describe.fixed


def test_metrics_compare_the_two_arms(tmp_path):
    from autef2.eval.metrics import compute_metrics, render_markdown

    config = make_config(tmp_path)

    def run(repairs, workspace_suffix):
        cfg = AutefConfig(
            workspace=tmp_path / workspace_suffix,
            use_venv=False,
            use_signature_cache=False,
            api_key="stub",
        )
        return run_project(SAMPLE_PROJECT, cfg, llm=scripted(cfg, repairs))

    good = run(
        {
            "test_multiply_returns_product": [GOOD_MULTIPLY],
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
        "a",
    )
    poor = run(
        {
            # Enough useless replies to outlast any escalation budget: when the
            # list runs dry the stub answers NO_TEST_FIX_NEEDED, which would
            # score this as a judged skip rather than the failure to repair
            # this arm is meant to represent.
            "test_multiply_returns_product": [USELESS_MULTIPLY] * 12,
            "test_describe_uses_precision": [GOOD_DESCRIBE],
        },
        "b",
    )

    metrics = {
        "autef2": compute_metrics("autef2", [good]),
        "weak": compute_metrics("weak", [poor]),
    }
    assert metrics["autef2"].fixed == 2
    assert metrics["weak"].fixed == 1
    assert metrics["autef2"].fix_rate > metrics["weak"].fix_rate

    markdown = render_markdown(metrics, {"autef2": [good], "weak": [poor]})
    assert "Fix rate" in markdown
    assert "Weakening rate" in markdown


# ---------------------------------------------------------------------------
# benchmark harness
# ---------------------------------------------------------------------------


def test_benchmark_runs_both_arms_on_identical_state(tmp_path):
    """The harness mechanics: same start, same env, artefacts, pristine restore."""
    import csv

    from autef2.eval.benchmark import ProjectSpec, run_benchmark

    config = make_config(tmp_path)
    llm = scripted(
        config,
        {
            "test_multiply_returns_product": [GOOD_MULTIPLY] * 4,
            "test_describe_uses_precision": [GOOD_DESCRIBE] * 4,
        },
    )
    spec = ProjectSpec(name="calcpkg", source=str(SAMPLE_PROJECT), stratum="tiny")

    result = run_benchmark(
        [spec], config, output_dir=tmp_path / "out", llm=llm
    )

    assert set(result.reports_by_arm) == {"baseline", "autef2"}
    # Both arms were offered the same failing tests -- that is what makes the
    # observations paired.
    baseline_ids = {r.nodeid for r in result.reports_by_arm["baseline"][0].records}
    autef2_ids = {r.nodeid for r in result.reports_by_arm["autef2"][0].records}
    assert baseline_ids == autef2_ids and len(baseline_ids) == 2

    for name in ("benchmark.md", "benchmark.json", "observations.csv"):
        assert (tmp_path / "out" / name).is_file(), name

    with (tmp_path / "out" / "observations.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4  # 2 tests x 2 arms
    assert {r["arm"] for r in rows} == {"baseline", "autef2"}

    assert result.metrics_by_arm["autef2"].observations == 2
    assert "Weakening rate" in result.markdown()


def test_benchmark_seeds_faults_only_into_passing_tests(tmp_path):
    from autef2.eval.benchmark import ProjectSpec, run_benchmark
    from autef2.eval.faults import FILE_SCOPED_KINDS

    config = make_config(tmp_path)
    llm = scripted(config, {})
    spec = ProjectSpec(
        name="calcpkg", source=str(SAMPLE_PROJECT), stratum="tiny", inject=3
    )

    result = run_benchmark([spec], config, output_dir=tmp_path / "out", llm=llm)

    faults = result.faults_by_project.get("calcpkg", [])
    assert faults, "expected seeded faults"

    # The fixture ships four tests, two passing and two deliberately broken.
    # Naming one of them was over-specific: the invariant is that a fault never
    # lands in a test that was already failing, because a repair on such a test
    # cannot be attributed to the seeded fault or the pre-existing one.
    already_failing = {"test_describe_uses_precision", "test_multiply_returns_product"}
    for fault in faults:
        if fault.kind in FILE_SCOPED_KINDS:
            continue
        assert fault.enclosing_test not in already_failing, (
            f"seeded {fault.kind} into {fault.enclosing_test}, which was "
            "already failing"
        )
