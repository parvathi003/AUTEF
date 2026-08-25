"""AutoFix Agent: apply the repair, re-run the test, and decide if it held.

The verification step is the point. v1 generated a fix, wrote it to the file,
and moved on -- whether the test then passed was never checked, so a "fixed"
test in its report meant only "the model returned something".

Here a repair is provisional until it is re-run. Four things can disqualify it,
and each one restores the file exactly as it was so the next rung starts clean:

* the returned code does not parse, or is not the function we asked for;
* the test still fails;
* the test passes, but only because its assertions were weakened;
* the test passes, but something that was passing now fails.

The last two are why a fix rate on its own is not a result. Both are recorded
rather than silently discarded, because "how often a fix passes by weakening
the assertion" is one of the numbers being reported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..config import AutefConfig
from ..context import ContextBuilder
from ..guards import compare as compare_assertions
from ..guards import detect_regressions
from ..llm import LLMClient, LLMError
from ..models import (
    Diagnosis,
    ProjectLayout,
    RepairAttempt,
    Strategy,
    SuiteResult,
    TestFailure,
)
from ..patcher import (
    FunctionSpan,
    PatchError,
    find_function,
    replace_file,
    replace_function,
    strip_code_fences,
    Snapshot,
)
from ..resolver import resolve
from ..runner import TestRunner
from ..strategies import NO_FIX_SENTINEL

logger = logging.getLogger(__name__)


@dataclass
class AttemptOutcome:
    """What one rung produced."""

    record: RepairAttempt
    accepted: bool
    new_failure: Optional[TestFailure] = None
    #: Set when the model asserts the source, not the test, is at fault.
    source_defect_claimed: bool = False


class AutoFixAgent:
    """Generates a repair, applies it, and verifies it survived."""

    def __init__(
        self,
        llm: Optional[LLMClient],
        config: AutefConfig,
        layout: ProjectLayout,
        runner: TestRunner,
        context_builder: ContextBuilder,
    ):
        self.llm = llm
        self.config = config
        self.layout = layout
        self.runner = runner
        self.context = context_builder

    def attempt(
        self,
        failure: TestFailure,
        diagnosis: Diagnosis,
        strategy: Strategy,
        attempt_number: int,
        *,
        baseline_passing: Sequence[str] = (),
        previous_notes: Sequence[str] = (),
    ) -> AttemptOutcome:
        started = time.time()
        record = RepairAttempt(
            attempt=attempt_number,
            strategy_id=strategy.id,
            strategy_label=strategy.label,
        )

        if not failure.test_file:
            return self._reject(
                record, started, "no test file could be resolved for this failure"
            )

        span = self._locate(failure)
        if span is None and strategy.scope == "function":
            return self._reject(
                record,
                started,
                f"function {failure.test_function!r} not found in {failure.test_file}",
            )

        # -- generate -----------------------------------------------------
        if self.llm is None:
            return self._reject(record, started, "no model configured")

        scoped = self.llm.scoped()
        prompt = self.context.build(
            failure, strategy, span, previous_attempts=previous_notes
        )
        try:
            reply = scoped.complete(
                [
                    {"role": "system", "content": strategy.system_prompt},
                    {"role": "user", "content": prompt},
                ]
            )
        except LLMError as exc:
            self._bill(record, scoped)
            return self._reject(record, started, f"model call failed: {exc}")

        self._bill(record, scoped)

        if NO_FIX_SENTINEL in reply:
            outcome = self._reject(
                record,
                started,
                "model reports the source is at fault, not the test",
            )
            outcome.source_defect_claimed = True
            return outcome

        code = strip_code_fences(reply)
        if not code.strip():
            return self._reject(record, started, "model returned nothing")

        # -- apply --------------------------------------------------------
        snapshot = Snapshot()
        snapshot.capture(failure.test_file)
        before_source = span.source if span else ""

        try:
            if strategy.scope == "file":
                replace_file(failure.test_file, code)
            else:
                replace_function(span, code)  # type: ignore[arg-type]
        except PatchError as exc:
            snapshot.restore()
            return self._reject(record, started, f"patch rejected: {exc}")

        record.applied = True
        record.patch_preview = _preview(code)

        # -- verify -------------------------------------------------------
        verification = self.runner.run_node(failure.nodeid)
        if not verification.ran:
            snapshot.restore()
            return self._reject(
                record, started, f"verification run failed: {verification.stdout_tail[-300:]}"
            )

        still_failing = next(
            (f for f in verification.failures if f.nodeid == failure.nodeid), None
        )
        if still_failing is not None or not verification.passed:
            new_failure = still_failing or _synthetic_failure(failure, verification)
            resolve(new_failure, self.layout)
            record.new_failure = (
                f"{new_failure.exception_type}: {new_failure.exception_message[:200]}"
            )
            snapshot.restore()
            record.duration_s = time.time() - started
            logger.info(
                "  attempt %d (%s): still failing -- %s",
                attempt_number, strategy.id, record.new_failure,
            )
            return AttemptOutcome(record=record, accepted=False, new_failure=new_failure)

        record.verified_pass = True

        # -- did it pass honestly? ----------------------------------------
        after_span = self._locate(failure)
        after_source = after_span.source if after_span else ""
        if before_source and after_source:
            record.weakening = compare_assertions(before_source, after_source)
        elif strategy.scope == "file" and after_source:
            original = snapshot.original(failure.test_file) or ""
            record.weakening = compare_assertions(
                _extract_from_text(original, failure) or after_source, after_source
            )

        if (
            self.config.reject_weakened_fixes
            and record.weakening
            and record.weakening.weakened
        ):
            snapshot.restore()
            reasons = "; ".join(record.weakening.reasons[:2])
            record.verified_pass = False
            return self._reject(
                record, started, f"fix weakened the test ({reasons})"
            )

        # -- did it break anything? ---------------------------------------
        regressions = self._check_regressions(failure, baseline_passing)
        record.regressions = regressions
        if regressions and self.config.reject_regressions:
            snapshot.restore()
            record.verified_pass = False
            return self._reject(
                record,
                started,
                f"fix broke {len(regressions)} previously passing test(s): "
                f"{', '.join(regressions[:3])}",
            )

        record.duration_s = time.time() - started
        logger.info(
            "  attempt %d (%s): verified pass", attempt_number, strategy.id
        )
        return AttemptOutcome(record=record, accepted=True)

    # -- helpers ----------------------------------------------------------

    def _locate(self, failure: TestFailure) -> Optional[FunctionSpan]:
        if not failure.test_file or not failure.test_function:
            return None
        return find_function(
            failure.test_file, failure.test_function, failure.test_class
        )

    def _check_regressions(
        self, failure: TestFailure, baseline_passing: Sequence[str]
    ) -> List[str]:
        """Re-run the neighbours the patch could plausibly have broken.

        Scoped to the edited file rather than the whole suite: a patch confined
        to one test file cannot break a test in another one, and a full-suite
        run per attempt would dominate the runtime of the whole evaluation.
        The final report re-runs everything, so a cross-file regression is
        still caught -- just at the end rather than per attempt.
        """
        if not baseline_passing or not failure.test_file:
            return []
        result = self.runner.run_file(failure.test_file)
        return detect_regressions(baseline_passing, result)

    def _bill(self, record: RepairAttempt, scoped: LLMClient) -> None:
        record.prompt_tokens = scoped.usage.prompt_tokens
        record.completion_tokens = scoped.usage.completion_tokens
        record.cost_usd = scoped.usage.cost_usd

    def _reject(
        self, record: RepairAttempt, started: float, reason: str
    ) -> AttemptOutcome:
        record.rejected_reason = reason
        record.duration_s = time.time() - started
        logger.info("  attempt %d (%s): %s", record.attempt, record.strategy_id, reason)
        return AttemptOutcome(record=record, accepted=False)


def _synthetic_failure(original: TestFailure, result: SuiteResult) -> TestFailure:
    """The node vanished from the run -- usually renamed or no longer collected."""
    if result.collection_errors:
        error = result.collection_errors[0]
        error.nodeid = original.nodeid
        return error
    return TestFailure(
        nodeid=original.nodeid,
        outcome=original.outcome,
        exception_type="CollectionError",
        exception_message=(
            "the test was not collected after the patch (renamed, removed, or "
            "moved out of its class)"
        ),
        longrepr=result.stdout_tail,
        phase="collect",
    )


def _extract_from_text(text: str, failure: TestFailure) -> Optional[str]:
    """Pull a function's source out of an in-memory file snapshot."""
    import ast

    if not failure.test_function:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == failure.test_function
        ):
            end = getattr(node, "end_lineno", node.lineno)
            return "\n".join(lines[node.lineno - 1 : end])
    return None


def _preview(code: str, limit: int = 8000) -> str:
    """The patched test function, kept whole.

    This is the evidence that a repair happened and what it changed, so it is
    stored in full rather than as a snippet. At 600 characters most repaired
    functions were cut mid-body, which is exactly where the interesting part
    is. The cap is a guard against a pathological reply, not a display budget.
    """
    code = code.strip()
    return code if len(code) <= limit else code[:limit] + "\n...[truncated]"
