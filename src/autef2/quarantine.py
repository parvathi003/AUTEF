"""Removing AUTEF's own unfixable tests from the suite it hands back.

Stage 4 writes tests the model guessed at. Most are right, the repair loop
fixes many of the rest, and a residue is simply wrong -- it asserts behaviour
the code does not have and no rung of the ladder could reconcile. Before this
module those tests stayed in the project forever. Two things followed, both
bad. The suite handed back at stage 9 was redder than the one uploaded, which
is the opposite of what a test-improvement framework is for. And stage 8
re-runs the suite, sees red, and refuses to score: AUTEF's own unreviewed
output gated AUTEF's own mutation stage off.

So a test this framework wrote, and could not make pass, is taken back out.

Two rules keep that honest:

*Only our own.* ``authored_here`` gates every removal. A test the project
shipped is never touched, however red it is -- it is evidence about the
project, and deleting it would destroy the user's work to flatter our own
numbers. Those failures are handled by targeting instead (see ``green_subset``).

*Nothing is destroyed.* The excised function is appended to a sidecar file
next to the test file it came from, with the diagnosis that condemned it. The
sidecar is deliberately not named ``test_*``, so pytest does not collect it,
and a reader can see exactly what was removed and why. A wrong guess that is
visible is a finding; a wrong guess that is deleted is a cover-up.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .agents.generation import authored_here
from .patcher import find_function

logger = logging.getLogger(__name__)

#: Header written once at the top of a sidecar file.
SIDECAR_HEADER = '''"""Tests AUTEF wrote, could not make pass, and therefore removed.

This file is not collected by pytest: the name does not match ``test_*.py``
and every function below is inert. It is kept so the removal is auditable.

Each entry records the test as it stood when it was taken out of the suite,
and the diagnosis that condemned it. A test here is one of two things: a wrong
guess by the generator, or a real defect in the code under test that the
generator described correctly and the repair loop was right to refuse to
paper over. The diagnosis line says which was believed at the time.
"""
'''


@dataclass
class QuarantinedTest:
    """One test taken out of the suite, and why."""

    nodeid: str
    test_file: str
    test_function: str
    reason: str
    sidecar: str
    root_cause: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "test_file": self.test_file,
            "test_function": self.test_function,
            "reason": self.reason,
            "sidecar": self.sidecar,
            "root_cause": self.root_cause,
        }


@dataclass
class QuarantineResult:
    """What one quarantine pass did."""

    removed: List[QuarantinedTest] = field(default_factory=list)
    #: Tests that were left alone, with the reason. Project-authored tests land
    #: here, and so does anything we could not locate in the file.
    kept: List[Dict[str, str]] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.removed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "removed": [q.to_dict() for q in self.removed],
            "kept": list(self.kept),
            "files_deleted": list(self.files_deleted),
        }


def sidecar_for(test_file: Path) -> Path:
    """Where the removed tests from ``test_file`` are kept.

    Named so pytest will not collect it: ``test_x.py`` becomes
    ``quarantined_x.py``, which matches neither ``test_*.py`` nor ``*_test.py``.
    """
    stem = test_file.stem
    for prefix in ("test_",):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    else:
        if stem.endswith("_test"):
            stem = stem[: -len("_test")]
    return test_file.parent / f"quarantined_{stem}.py"


def quarantine(
    test_file: str,
    test_function: str,
    *,
    nodeid: str,
    reason: str,
    test_class: Optional[str] = None,
    root_cause: Optional[str] = None,
) -> Optional[QuarantinedTest]:
    """Take one test function out of its file. Returns None if untouched.

    The caller is expected to have checked provenance, but this checks again:
    a bug here edits a file the framework does not own, which is the one
    mistake that must not be possible.
    """
    path = Path(test_file)
    if not authored_here(path):
        logger.debug("Not quarantining %s: not authored here", test_file)
        return None

    span = find_function(str(path), test_function, test_class)
    if span is None:
        logger.warning(
            "Could not locate %s in %s; leaving it in place",
            test_function, test_file,
        )
        return None

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable at this point is odd
        logger.warning("Could not read %s: %s", test_file, exc)
        return None

    start, end = span.start_line, span.end_line
    # Removing the last method of a TestCase would leave an empty class body,
    # which does not parse. Take the whole class in that case.
    if span.class_name:
        widened = _class_span_if_emptied(source, span.class_name, test_function)
        if widened is not None:
            start, end = widened

    lines = source.splitlines(keepends=True)
    excised = "".join(lines[start - 1:end])
    remaining = "".join(lines[: start - 1] + lines[end:])

    try:
        ast.parse(remaining)
    except SyntaxError as exc:
        logger.warning(
            "Excising %s from %s would not parse (%s); leaving it in place",
            test_function, path.name, exc,
        )
        return None

    sidecar = sidecar_for(path)
    _append_to_sidecar(sidecar, excised, nodeid=nodeid, reason=reason)
    path.write_text(remaining, encoding="utf-8")

    logger.info("Quarantined %s -> %s", nodeid, sidecar.name)
    return QuarantinedTest(
        nodeid=nodeid,
        test_file=str(path),
        test_function=test_function,
        reason=reason,
        sidecar=str(sidecar),
        root_cause=root_cause,
    )


def _class_span_if_emptied(
    source: str, class_name: str, method_name: str
) -> Optional[tuple]:
    """Bounds of ``class_name`` when ``method_name`` is its only real body.

    ``setUp``/``tearDown`` do not count as content: a TestCase holding nothing
    but fixtures collects no tests and is dead weight in the file.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - it parsed for find_function
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        survivors = [
            item for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name != method_name
            and item.name not in ("setUp", "tearDown", "setUpClass", "tearDownClass")
        ]
        if survivors:
            return None
        start = node.lineno
        for decorator in node.decorator_list:
            start = min(start, decorator.lineno)
        return (start, getattr(node, "end_lineno", node.lineno))
    return None


def _append_to_sidecar(
    sidecar: Path, excised: str, *, nodeid: str, reason: str
) -> None:
    existing = ""
    if sidecar.exists():
        try:
            existing = sidecar.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            existing = ""
    parts = [existing] if existing else [SIDECAR_HEADER]
    body = "\n".join(f"# {line}" for line in excised.rstrip().splitlines())
    parts.append(
        f"\n\n# --- removed: {nodeid}\n"
        f"# diagnosis: {reason}\n"
        f"{body}\n"
    )
    try:
        sidecar.write_text("".join(parts), encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        logger.warning("Could not write %s: %s", sidecar, exc)


def has_tests(path: Path) -> bool:
    """Does this file still define anything pytest would collect?"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):  # pragma: no cover - treat as "leave alone"
        return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return True
    return False


def sweep(records: Sequence[Any]) -> QuarantineResult:
    """Quarantine every AUTEF-authored test the repair loop could not fix.

    ``records`` are ``RepairRecord``s from a finished run. A record qualifies
    when it is neither fixed nor still worth keeping: not ``fixed``, and
    carrying either a ``skipped_reason`` or an exhausted attempt list.

    A file emptied of tests by this is removed, since an empty generated file
    is noise in the project the user gets back.
    """
    result = QuarantineResult()
    emptied: Dict[str, Path] = {}

    for record in records:
        if getattr(record, "fixed", False):
            continue
        failure = getattr(record, "failure", None)
        test_file = getattr(record, "test_file", None) or getattr(
            failure, "test_file", None
        )
        test_function = getattr(record, "test_function", None) or getattr(
            failure, "test_function", None
        )
        nodeid = getattr(record, "nodeid", "?")

        if not test_file or not test_function:
            result.kept.append(
                {"nodeid": nodeid, "why": "could not resolve the test's location"}
            )
            continue
        if not authored_here(Path(test_file)):
            result.kept.append(
                {"nodeid": nodeid, "why": "the project's own test, not ours"}
            )
            continue

        diagnosis = getattr(record, "diagnosis", None)
        root_cause = getattr(getattr(diagnosis, "root_cause", None), "value", None)
        reason = (
            getattr(record, "skipped_reason", None)
            or f"repair exhausted after {len(getattr(record, 'attempts', []))} attempt(s)"
        )
        removed = quarantine(
            test_file,
            test_function,
            nodeid=nodeid,
            reason=reason,
            test_class=getattr(record, "test_class", None)
            or getattr(failure, "test_class", None),
            root_cause=root_cause,
        )
        if removed is None:
            result.kept.append(
                {"nodeid": nodeid, "why": "could not be located in its file"}
            )
            continue
        result.removed.append(removed)
        emptied[test_file] = Path(test_file)

    for path in emptied.values():
        if path.exists() and not has_tests(path):
            try:
                path.unlink()
                result.files_deleted.append(str(path))
                logger.info("Removed %s: nothing left to collect", path.name)
            except OSError:  # pragma: no cover
                pass

    return result
