"""Guards against fixes that pass for the wrong reason.

An automated repair loop optimises for "the test now passes", and the cheapest
way to make a test pass is to stop it from asserting anything. That failure
mode is invisible in a fix-rate number, which is exactly why it is one of the
reported metrics: *how often a fix passes only by weakening or deleting the
assertion*.

Two checks live here:

* **weakening** -- compare the assertion profile of the test function before
  and after the patch;
* **regression** -- compare the set of passing tests before and after, so a
  repair that greens one test by breaking two is not counted as a success.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set

from .models import SuiteResult, WeakeningReport

logger = logging.getLogger(__name__)

#: unittest assertion methods ranked by how much they actually constrain the
#: result. Swapping a high rank for a low one is a weakening even though the
#: assertion count is unchanged.
_ASSERT_SPECIFICITY = {
    "assertequal": 5, "assertnotequal": 5, "assertis": 5, "assertisnot": 5,
    "assertdictequal": 5, "assertlistequal": 5, "assertsetequal": 5,
    "asserttupleequal": 5, "assertmultilineequal": 5, "assertsequenceequal": 5,
    "assertalmostequal": 5, "assertcountequal": 5,
    "assertraises": 4, "assertraisesregex": 5, "assertwarns": 4,
    "assertin": 4, "assertnotin": 4, "assertisinstance": 3,
    "assertgreater": 4, "assertless": 4, "assertgreaterequal": 4,
    "assertlessequal": 4, "assertregex": 4,
    "assertisnone": 2, "assertisnotnone": 2,
    "asserttrue": 1, "assertfalse": 1,
}

_SKIP_TOKENS = ("skip", "skipif", "skiptest", "xfail", "expectedfailure")

_TRIVIAL_CONSTANTS = (True, False, None, 1, 0, "")


@dataclass
class AssertionProfile:
    """What a test function actually checks."""

    total: int = 0
    specificity: int = 0
    trivial: int = 0
    skipped: bool = False
    broad_raises: int = 0
    statements: int = 0
    methods: List[str] = field(default_factory=list)
    parse_error: Optional[str] = None

    @classmethod
    def empty(cls, reason: str) -> "AssertionProfile":
        return cls(parse_error=reason)


def profile_function(source: str) -> AssertionProfile:
    """Build an assertion profile from a function's source text."""
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError as exc:
        return AssertionProfile.empty(str(exc))

    profile = AssertionProfile()

    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            profile.statements += 1

        if isinstance(node, ast.Assert):
            profile.total += 1
            profile.methods.append("assert")
            profile.specificity += 1 if _is_trivial_test(node.test) else 4
            if _is_trivial_test(node.test):
                profile.trivial += 1

        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if not name:
                continue
            lowered = name.split(".")[-1].lower()

            if lowered in _ASSERT_SPECIFICITY:
                profile.total += 1
                profile.methods.append(lowered)
                profile.specificity += _ASSERT_SPECIFICITY[lowered]
                if _all_trivial(node.args):
                    profile.trivial += 1

            elif lowered.startswith("assert_"):  # mock: assert_called_with etc.
                profile.total += 1
                profile.methods.append(lowered)
                profile.specificity += 4 if "with" in lowered else 2

            elif lowered in ("raises", "warns") and _is_broad_exception(node):
                profile.broad_raises += 1

            elif lowered in ("skip", "skiptest", "xfail"):
                profile.skipped = True

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                text = (_call_name(decorator) or "").lower()
                if any(token in text for token in _SKIP_TOKENS):
                    profile.skipped = True

    return profile


def compare(before: str, after: str) -> WeakeningReport:
    """Decide whether ``after`` is a genuine repair or a gutted test."""
    old = profile_function(before)
    new = profile_function(after)

    report = WeakeningReport(
        asserts_before=old.total,
        asserts_after=new.total,
        skipped=new.skipped and not old.skipped,
    )
    reasons: List[str] = []

    if new.parse_error:
        reasons.append(f"patched test does not parse: {new.parse_error}")

    if report.skipped:
        reasons.append("the repaired test is skipped or marked xfail")

    if new.total == 0 and old.total > 0:
        reasons.append("all assertions were removed")
    elif new.total < old.total:
        reasons.append(
            f"assertion count fell from {old.total} to {new.total}"
        )

    if new.total and new.trivial >= new.total:
        reasons.append("every remaining assertion is trivially true")
    elif new.trivial > old.trivial:
        reasons.append(
            f"trivial assertions rose from {old.trivial} to {new.trivial}"
        )

    # Same number of assertions, but each one checks less.
    if old.total and new.total >= old.total:
        old_mean = old.specificity / old.total
        new_mean = new.specificity / max(new.total, 1)
        if new_mean < old_mean - 0.75:
            reasons.append(
                f"assertions became less specific (mean strength "
                f"{old_mean:.1f} -> {new_mean:.1f}), e.g. "
                f"{_diff_methods(old.methods, new.methods)}"
            )

    if new.broad_raises > old.broad_raises:
        reasons.append("an expected exception was broadened to bare Exception")

    # A body that collapses is suspicious even when the assert count survives.
    if old.statements >= 4 and new.statements < old.statements * 0.4:
        reasons.append(
            f"test body shrank from {old.statements} to {new.statements} statements"
        )

    report.reasons = reasons
    report.weakened = bool(reasons)
    return report


def detect_regressions(
    baseline_passing: Iterable[str], current: SuiteResult
) -> List[str]:
    """Tests that passed before the repair and do not pass now."""
    if not current.ran:
        return []
    baseline: Set[str] = set(baseline_passing)
    if not baseline:
        return []

    now_failing = set(current.failing_ids)
    # Only judge tests the current run actually covered; a partial run (one
    # file) says nothing about tests it did not execute.
    observed = set(current.passed) | now_failing | set(current.skipped)
    return sorted(baseline & now_failing & observed)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return None


def _is_trivial_test(node: ast.AST) -> bool:
    """``assert True``, ``assert 1``, ``assert x == x``."""
    if isinstance(node, ast.Constant):
        return node.value in _TRIVIAL_CONSTANTS or bool(node.value) is True
    if isinstance(node, ast.Compare) and len(node.comparators) == 1:
        return ast.dump(node.left) == ast.dump(node.comparators[0])
    return False


def _all_trivial(args: Sequence[ast.expr]) -> bool:
    if not args:
        return False
    if len(args) == 1:
        return isinstance(args[0], ast.Constant) and bool(args[0].value) is True
    if len(args) >= 2:
        first, second = args[0], args[1]
        if isinstance(first, ast.Constant) and isinstance(second, ast.Constant):
            return first.value == second.value
        return ast.dump(first) == ast.dump(second)
    return False


def _is_broad_exception(node: ast.Call) -> bool:
    for arg in node.args:
        name = _call_name(arg)
        if name in ("Exception", "BaseException"):
            return True
    return False


def _diff_methods(before: List[str], after: List[str]) -> str:
    lost = [m for m in before if m not in after][:2]
    gained = [m for m in after if m not in before][:2]
    if lost and gained:
        return f"{'/'.join(lost)} -> {'/'.join(gained)}"
    if lost:
        return f"dropped {'/'.join(lost)}"
    return "assertions changed"
