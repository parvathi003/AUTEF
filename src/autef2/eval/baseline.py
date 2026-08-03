"""The baseline arm: v1's repair, reproduced faithfully.

v1's repair loop, from ``agenticapp/agents/AutoFixingAgent.py``:

    for each failing test:
        locate the test file
        extract the failing function
        send (source, function, error string) to ONE prompt
        write whatever comes back
        move on

No diagnosis, one attempt, no verification, no alternative strategy. The prompt
below is v1's ``build_fix_prompt`` verbatim, so the arm is measuring v1's actual
instruction rather than a paraphrase of it.

What is *not* reproduced: v1's filename-guessing file resolution, its regex
function extraction, and its habit of appending the repaired function to the
end of the file. Those belong to the portability claim, not the repair claim,
and reproducing them here would hand the comparison a result it did not earn --
v1 would lose to a file-corruption bug rather than to its prompt. Both arms
therefore share v2's resolver and AST patcher.
"""

from __future__ import annotations

import logging
import re
import time
from typing import List, Optional, Sequence

from ..config import AutefConfig
from ..guards import compare as compare_assertions
from ..llm import LLMClient, LLMError
from ..models import (
    ProjectLayout,
    RepairAttempt,
    RepairRecord,
    RunReport,
    TestFailure,
)
from ..patcher import (
    PatchError,
    Snapshot,
    find_function,
    replace_function,
    strip_code_fences,
)
from ..resolver import resolve_all
from ..runner import TestRunner
from ..venv_manager import Environment

logger = logging.getLogger(__name__)

MAX_SOURCE_CHARS = 12_000


def build_fix_prompt(source_code: str, test_function_code: str, error_reason: str):
    """v1's prompt, unchanged apart from formatting."""
    return [
        {
            "role": "system",
            "content": (
                "You are a Python expert. You are given a source file, a failing "
                "test function that tests it, and an error message. Fix the test "
                "function. Do not return explanations or formatting - just return "
                "the corrected test function code."
            ),
        },
        {
            "role": "user",
            "content": f""" Source Code:{source_code}
                            Broken Test Function:{test_function_code}
                            Error Message:{error_reason}
                            ### Expected Output Format:
                             -Only the updated functions** without any extra text, instructions, or explanations.
                             -Do not include any markdown formatting such as ``` or ```python
                             -Do NOT generate class headers (`class TestXYZ`) or import statements.
                             -Each test must be a properly formatted Python function starting with `def test_...`
                             -Each test method should have a descriptive name and include the 'self' parameter. (e.g. 'def test_example(self):')
                        """,
        },
    ]


def apply_regex_fix(test_function_code: str, error_reason: str) -> Optional[str]:
    """v1's pre-LLM regex fixes.

    Off by default. These are hand-tuned to one application -- ``.name`` is
    rewritten to ``.item`` on any AttributeError -- so leaving them on would
    mostly measure how closely a benchmark project resembles the sample app
    they were written for.
    """
    if "AssertionError" in error_reason and "!=" in error_reason:
        return re.sub(r"{(\d+)}", r"\1", test_function_code)
    if "AttributeError" in error_reason:
        return re.sub(r"\.name", ".item", test_function_code)
    return None


class BaselineOrchestrator:
    """One generic prompt, one attempt per failing test, no verification."""

    def __init__(
        self,
        layout: ProjectLayout,
        environment: Environment,
        config: AutefConfig,
        llm: Optional[LLMClient],
        *,
        use_regex_fixes: bool = False,
    ):
        self.layout = layout
        self.environment = environment
        self.config = config
        self.llm = llm
        self.use_regex_fixes = use_regex_fixes
        self.runner = TestRunner(layout, environment, config)

    def run(self, *, max_tests: Optional[int] = None) -> RunReport:
        started = time.time()
        report = RunReport(project=self.layout.name, layout=self.layout, arm="baseline")

        before = self.runner.run_suite()
        report.before = before
        if not before.ran:
            report.error = "The test suite could not be executed."
            report.duration_s = time.time() - started
            return report

        failures = resolve_all(
            list(before.failures) + list(before.collection_errors), self.layout
        )
        if max_tests is not None:
            failures = failures[:max_tests]

        for index, failure in enumerate(failures, start=1):
            logger.info("[%d/%d] %s (baseline)", index, len(failures), failure.nodeid)
            report.records.append(self._repair(failure))

        # v1 never checked its own work; the harness checks it from outside so
        # the arm can be scored on the same footing as the other one.
        report.after = self.runner.run_suite()
        _score_against_suite(report)

        if self.llm is not None:
            report.prompt_tokens = self.llm.usage.prompt_tokens
            report.completion_tokens = self.llm.usage.completion_tokens
            report.llm_calls = self.llm.usage.calls
            report.cost_usd = self.llm.usage.cost_usd
        report.duration_s = time.time() - started
        return report

    # -- one attempt, no more --------------------------------------------

    def _repair(self, failure: TestFailure) -> RepairRecord:
        record = RepairRecord(nodeid=failure.nodeid, signature=failure.signature())
        attempt = RepairAttempt(
            attempt=1, strategy_id="generic_prompt", strategy_label="v1 generic fix prompt"
        )
        started = time.time()

        span = (
            find_function(failure.test_file, failure.test_function, failure.test_class)
            if failure.test_file and failure.test_function
            else None
        )
        if span is None:
            attempt.rejected_reason = "failing function could not be located"
            record.attempts.append(attempt)
            return record

        source_code = _read_sources(failure.source_files)
        error_reason = failure.longrepr or failure.exception_message

        fixed = (
            apply_regex_fix(span.source, error_reason) if self.use_regex_fixes else None
        )

        if fixed is None:
            if self.llm is None:
                attempt.rejected_reason = "no model configured"
                record.attempts.append(attempt)
                return record
            scoped = self.llm.scoped()
            try:
                fixed = scoped.complete(
                    build_fix_prompt(source_code, span.source, error_reason)
                )
            except LLMError as exc:
                attempt.rejected_reason = f"model call failed: {exc}"
                attempt.duration_s = time.time() - started
                record.attempts.append(attempt)
                return record
            attempt.prompt_tokens = scoped.usage.prompt_tokens
            attempt.completion_tokens = scoped.usage.completion_tokens
            attempt.cost_usd = scoped.usage.cost_usd

        code = strip_code_fences(fixed or "")
        if not code.strip() or code.strip() == span.source.strip():
            attempt.rejected_reason = "model returned nothing new"
            attempt.duration_s = time.time() - started
            record.attempts.append(attempt)
            return record

        snapshot = Snapshot()
        snapshot.capture(failure.test_file)
        try:
            replace_function(span, code)
        except PatchError as exc:
            attempt.rejected_reason = f"patch rejected: {exc}"
            attempt.duration_s = time.time() - started
            record.attempts.append(attempt)
            return record

        # Written and accepted without checking. That is the point of the arm.
        attempt.applied = True
        attempt.patch_preview = code[:600]

        # The arm does not act on this, but it is measured for both arms
        # identically -- weakening is a comparison of the code before and
        # after, so it needs no re-run and costs the baseline nothing.
        after_span = find_function(
            failure.test_file, failure.test_function, failure.test_class
        )
        if after_span is not None:
            attempt.weakening = compare_assertions(span.source, after_span.source)

        attempt.duration_s = time.time() - started
        record.attempts.append(attempt)
        record.final_strategy_id = "generic_prompt"
        return record


def _score_against_suite(report: RunReport) -> None:
    """Mark records fixed based on the post-run suite, plus weakening.

    The baseline arm cannot set ``fixed`` itself -- it never re-runs anything --
    so the harness determines it afterwards from the final suite result.
    """
    if report.after is None:
        return
    passing = set(report.after.passed)
    still_failing = set(report.after.failing_ids)

    for record in report.records:
        if record.nodeid in passing:
            record.fixed = True
        elif record.nodeid not in still_failing:
            # Neither passing nor failing: the patch removed it from collection.
            record.fixed = False
            if record.attempts:
                record.attempts[-1].new_failure = (
                    "test no longer collected after the patch"
                )


def _read_sources(paths: Sequence[str]) -> str:
    from pathlib import Path

    chunks: List[str] = []
    budget = MAX_SOURCE_CHARS
    for path in paths:
        if budget <= 0:
            break
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text[:budget])
        budget -= len(text[:budget])
    return "\n\n".join(chunks)
