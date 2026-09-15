"""Failure Analysis Agent: why did this test fail?

v1 never asked. Every failure went straight to one fix prompt carrying the raw
error string, so the repair had to infer the cause and the fix in a single
step, with no record of what it concluded.

Diagnosis here is two-stage. A deterministic classifier reads the exception
type, the failing phase and a set of message signatures, and is right on its
own for the unambiguous cases (ModuleNotFoundError is an import error; a
collection-phase failure is a collection error). The model is then asked to
confirm or correct it with the traceback and source in view, which is what
catches the cases the exception type alone cannot separate -- most importantly
whether the test is wrong or *the code is*.

If the model is unavailable or unsure, the heuristic stands. Diagnosis never
becomes a single point of failure.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from ..config import AutefConfig
from ..llm import LLMClient, LLMError
from ..models import Diagnosis, ProjectLayout, RootCause, TestFailure

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are a Python test-failure analyst. You are given a failing test, its
traceback, and the source it exercises. Identify the ROOT CAUSE. Do not propose
a fix.

Choose exactly one root_cause:
- import_error: a module or name cannot be imported.
- collection_error: the test file cannot be imported or parsed at all.
- assertion_mismatch: the code ran, but produced a value the test did not expect.
- mock_misconfiguration: a mock or patch is wrong -- wrong patch target, wrong
  return_value/side_effect, or a failing call assertion on a mock.
- fixture_setup_error: the failure happened in a fixture, setUp or setup_method,
  before the test body ran.
- api_misuse: the test calls the code incorrectly -- wrong signature, wrong
  argument types, or an attribute/method that does not exist.
- environment_dependency: the test needs something absent here -- a database,
  a network service, a native library, a missing data file.
- production_bug: the test is CORRECT and the SOURCE CODE is wrong.
- flaky_nondeterminism: the outcome depends on time, randomness, ordering or
  state left by another test.
- unknown: the evidence does not support any of the above.

Set at_fault to "test", "source", "environment" or "unknown".

Judge production_bug seriously. If the test encodes the behaviour the source
clearly intends and the source contradicts it, say production_bug -- editing
the test would hide a real defect. But do not reach for it merely because the
test looks reasonable; the test being wrong is the more common case.

Set confidence between 0 and 1, and mean it. Below 0.5 says the traceback does
not really tell you.

Reply with a single JSON object:
{"root_cause": "...", "at_fault": "...", "confidence": 0.0,
 "explanation": "one or two sentences", "evidence": ["quoted line from the traceback"]}
""".strip()


# ---------------------------------------------------------------------------
# deterministic pre-classification
# ---------------------------------------------------------------------------

#: (compiled pattern, cause, confidence). First match wins, so order matters:
#: the narrow mock/environment signatures must be tested before the broad
#: exception-type rules that would otherwise swallow them.
_MESSAGE_RULES: List[Tuple[re.Pattern, RootCause, float]] = [
    (re.compile(r"fixture ['\"].+['\"] not found", re.I), RootCause.FIXTURE_SETUP_ERROR, 0.95),
    (re.compile(r"(expected call not found|Expected(?: '.*')? to (?:have been )?call|assert_(?:called|any_call|has_calls|not_called))", re.I), RootCause.MOCK_MISCONFIGURATION, 0.9),
    (re.compile(r"(Mock|MagicMock|AsyncMock) object has no attribute", re.I), RootCause.MOCK_MISCONFIGURATION, 0.9),
    (re.compile(r"does not have the attribute|_patch_object|Cannot autospec", re.I), RootCause.MOCK_MISCONFIGURATION, 0.85),
    (re.compile(r"<(Magic)?Mock (id|name)=", re.I), RootCause.MOCK_MISCONFIGURATION, 0.7),
    (re.compile(r"(Connection refused|Failed to establish a new connection|Name or service not known|getaddrinfo failed|Temporary failure in name resolution)", re.I), RootCause.ENVIRONMENT_DEPENDENCY, 0.9),
    # Service names are matched only as the module of a raised exception
    # (``redis.exceptions.ConnectionError``) or next to a connection word. Bare
    # "Redis", "Kafka" and "docker" used to match anywhere in the traceback, so
    # a test whose name or docstring mentioned Docker was classified as an
    # environment dependency -- which is NON_REPAIRABLE, so it was skipped
    # permanently, without one repair attempt.
    (re.compile(r"\b(redis|kafka|docker|pymongo|botocore|boto3)\.[\w.]*(error|exception)", re.I), RootCause.ENVIRONMENT_DEPENDENCY, 0.85),
    (re.compile(r"\b(redis|kafka|docker|rabbitmq|memcached)\b[^\n]{0,60}\b(refused|unreachable|unavailable|not running|timed out|could not connect)\b", re.I), RootCause.ENVIRONMENT_DEPENDENCY, 0.8),
    (re.compile(r"(could not connect to server|OperationalError|Access denied for user|no such table)", re.I), RootCause.ENVIRONMENT_DEPENDENCY, 0.75),
    (re.compile(r"(takes \d+ positional argument|missing \d+ required|unexpected keyword argument|got multiple values for)", re.I), RootCause.API_MISUSE, 0.85),
]

#: Exception type -> cause, for the cases where the type alone settles it.
_TYPE_RULES = {
    "ModuleNotFoundError": (RootCause.IMPORT_ERROR, 0.95),
    "ImportError": (RootCause.IMPORT_ERROR, 0.9),
    "SyntaxError": (RootCause.COLLECTION_ERROR, 0.95),
    "IndentationError": (RootCause.COLLECTION_ERROR, 0.95),
    "TabError": (RootCause.COLLECTION_ERROR, 0.95),
    "AssertionError": (RootCause.ASSERTION_MISMATCH, 0.7),
    "Failed": (RootCause.ASSERTION_MISMATCH, 0.6),
    "TypeError": (RootCause.API_MISUSE, 0.6),
    "AttributeError": (RootCause.API_MISUSE, 0.55),
    "NameError": (RootCause.IMPORT_ERROR, 0.5),
    "FixtureLookupError": (RootCause.FIXTURE_SETUP_ERROR, 0.95),
    "ConnectionError": (RootCause.ENVIRONMENT_DEPENDENCY, 0.85),
    "ConnectionRefusedError": (RootCause.ENVIRONMENT_DEPENDENCY, 0.9),
    "TimeoutError": (RootCause.ENVIRONMENT_DEPENDENCY, 0.6),
}


def error_text(failure: TestFailure) -> str:
    """The error itself, without the test source that led up to it.

    pytest's ``longrepr`` interleaves the failing test's own source with the
    error, and every line of that source used to be searched. A test whose
    *passing* setup called ``mock.assert_called_once()`` three lines above the
    real failure was therefore classified MOCK_MISCONFIGURATION at 0.9
    confidence, on the strength of code that worked. Only the ``E`` lines are
    the error; the rest is context.

    Falls back to the whole thing when there are no ``E`` lines, which is how
    collection errors arrive.
    """
    marked = [
        line for line in failure.longrepr.splitlines()
        if line.lstrip().startswith("E ") or line.lstrip() == "E"
    ]
    body = "\n".join(marked) if marked else failure.longrepr
    return f"{failure.exception_type}\n{failure.exception_message}\n{body}"


def heuristic_classify(failure: TestFailure) -> Tuple[RootCause, float]:
    """Classify without calling the model. Cheap, deterministic, and the
    fallback whenever the model is unavailable or unsure."""
    haystack = error_text(failure)

    if failure.phase == "collect":
        return RootCause.COLLECTION_ERROR, 0.9

    for pattern, cause, confidence in _MESSAGE_RULES:
        if pattern.search(haystack):
            return cause, confidence

    if failure.phase in ("setup", "teardown"):
        return RootCause.FIXTURE_SETUP_ERROR, 0.8

    exception_type = (failure.exception_type or "").split(".")[-1]
    if exception_type in _TYPE_RULES:
        return _TYPE_RULES[exception_type]

    if failure.exception_message.strip().startswith("assert"):
        return RootCause.ASSERTION_MISMATCH, 0.7

    return RootCause.UNKNOWN, 0.3


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


class FailureAnalysisAgent:
    """Names the root cause of a failure."""

    def __init__(self, llm: Optional[LLMClient], config: AutefConfig):
        self.llm = llm
        self.config = config

    def diagnose(
        self,
        failure: TestFailure,
        layout: ProjectLayout,
        context: str,
    ) -> Diagnosis:
        heuristic_cause, heuristic_confidence = heuristic_classify(failure)

        if self.llm is None:
            return Diagnosis(
                root_cause=heuristic_cause,
                confidence=heuristic_confidence,
                at_fault=_default_fault(heuristic_cause),
                explanation="Heuristic classification (no model configured).",
                heuristic_cause=heuristic_cause,
                llm_agreed=False,
                model_answered=False,
            )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{context}\n\n"
                    f"A static classifier suggests '{heuristic_cause.value}' "
                    f"(confidence {heuristic_confidence:.2f}). Confirm it or "
                    f"correct it based on the evidence above."
                ),
            },
        ]

        try:
            data = self.llm.complete_json(
                messages, required_keys=("root_cause", "confidence")
            )
        except LLMError as exc:
            logger.warning("Diagnosis call failed (%s); using heuristic", exc)
            return Diagnosis(
                root_cause=heuristic_cause,
                confidence=heuristic_confidence,
                at_fault=_default_fault(heuristic_cause),
                explanation=f"Heuristic classification (model unavailable: {exc}).",
                heuristic_cause=heuristic_cause,
                llm_agreed=False,
                model_answered=False,
            )

        cause = _parse_cause(data.get("root_cause"))
        confidence = _parse_confidence(data.get("confidence"))

        if cause is None:
            cause, confidence = heuristic_cause, heuristic_confidence

        # The heuristic wins when the model is genuinely unsure and the
        # heuristic is not. Trusting a 0.3-confidence relabel over a 0.95
        # ModuleNotFoundError match would be strictly worse.
        if confidence < 0.5 and heuristic_confidence >= 0.8:
            logger.debug(
                "Keeping heuristic %s over low-confidence %s",
                heuristic_cause.value, cause.value,
            )
            cause, confidence = heuristic_cause, heuristic_confidence

        evidence = data.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]

        return Diagnosis(
            root_cause=cause,
            confidence=confidence,
            at_fault=str(data.get("at_fault") or _default_fault(cause)),
            explanation=str(data.get("explanation") or "").strip(),
            evidence=[str(e) for e in evidence][:5],
            heuristic_cause=heuristic_cause,
            llm_agreed=(cause == heuristic_cause),
        )


def _parse_cause(raw) -> Optional[RootCause]:
    if not raw:
        return None
    text = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    for cause in RootCause:
        if cause.value == text:
            return cause
    return None


def _parse_confidence(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, value))


def _default_fault(cause: RootCause) -> str:
    if cause == RootCause.PRODUCTION_BUG:
        return "source"
    if cause == RootCause.ENVIRONMENT_DEPENDENCY:
        return "environment"
    if cause == RootCause.UNKNOWN:
        return "unknown"
    return "test"
