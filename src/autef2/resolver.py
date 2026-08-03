"""Find the files involved in a failure, from evidence rather than convention.

This replaces v1's ``locate_test_file`` / ``locate_source_file``, which took a
test id like ``tests.InsuranceApp.TestPolicyService.test_x``, converted
``TestPolicyService`` from CamelCase to ``policy_service``, and walked the tree
looking for ``test_policy_service*.py`` and ``policy_service.py``. That works
only where the test class name happens to encode the filename -- true of the
sample app it was written against, false in general, and silently wrong (it
returns None and the repair is skipped) rather than loudly wrong.

Two sources of truth are used instead:

* the pytest **nodeid**, which literally contains the test file path, class and
  function -- no inference at all;
* the **traceback frames**, which name every file that participated in the
  failure, so the code under test is identified by having been executed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

from .models import ProjectLayout, TestFailure

logger = logging.getLogger(__name__)

#: Frames in these locations are library internals, never the code under test.
_LIBRARY_MARKERS = (
    "site-packages", "dist-packages", "lib/python", "lib\\python",
    os.sep + "unittest" + os.sep, os.sep + "_pytest" + os.sep,
    os.sep + "pluggy" + os.sep,
)


def resolve(failure: TestFailure, layout: ProjectLayout) -> TestFailure:
    """Fill in ``test_file``, ``test_function``, ``test_class``, ``source_files``."""
    root = Path(layout.root).resolve()

    failure.test_file = _resolve_test_file(failure, root)
    failure.test_class, failure.test_function = _parse_nodeid_parts(failure.nodeid)
    failure.source_files = _resolve_source_files(failure, layout, root)

    if failure.test_file is None:
        logger.debug("Could not resolve a test file for %s", failure.nodeid)
    return failure


def resolve_all(failures: Sequence[TestFailure], layout: ProjectLayout) -> List[TestFailure]:
    return [resolve(f, layout) for f in failures]


# ---------------------------------------------------------------------------
# test file
# ---------------------------------------------------------------------------


def _resolve_test_file(failure: TestFailure, root: Path) -> Optional[str]:
    """The nodeid's path component is authoritative; frames are the fallback."""
    node_path = failure.nodeid.split("::", 1)[0].strip()
    if node_path:
        candidate = (root / node_path).resolve()
        if candidate.is_file():
            return str(candidate)
        # Collection errors sometimes carry an absolute nodeid.
        absolute = Path(node_path)
        if absolute.is_absolute() and absolute.is_file():
            return str(absolute.resolve())

    # No usable nodeid (import-time crash): take the first project frame whose
    # filename looks like a test module.
    for frame in failure.frames:
        path = _absolute(frame.path, root)
        if path and _within(path, root) and _looks_like_test(path):
            return str(path)
    return None


def _looks_like_test(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _parse_nodeid_parts(nodeid: str) -> tuple[Optional[str], Optional[str]]:
    """``tests/test_x.py::TestA::test_b[case-1]`` -> ("TestA", "test_b")."""
    parts = nodeid.split("::")[1:]
    if not parts:
        return None, None
    function = parts[-1]
    # Strip the parametrisation id; the function in the file is unparametrised.
    if "[" in function:
        function = function.split("[", 1)[0]
    test_class = parts[-2] if len(parts) >= 2 else None
    return test_class, function


# ---------------------------------------------------------------------------
# source files
# ---------------------------------------------------------------------------


def _resolve_source_files(
    failure: TestFailure, layout: ProjectLayout, root: Path
) -> List[str]:
    """Project files that took part in the failure, most relevant first.

    Relevance order: files under a detected source root beat files elsewhere in
    the project, and among equals the deepest frame -- the one closest to where
    the exception was actually raised -- wins.
    """
    source_roots = [Path(p).resolve() for p in layout.source_roots]
    test_file = Path(failure.test_file).resolve() if failure.test_file else None

    scored: List[tuple[int, int, str]] = []
    for depth, frame in enumerate(failure.frames):
        path = _absolute(frame.path, root)
        if path is None or not path.is_file():
            continue
        if not _within(path, root) or _is_library(path):
            continue
        if test_file is not None and path == test_file:
            continue
        if _looks_like_test(path):
            continue  # a helper test module, not the code under test

        in_source_root = any(_within(path, sr) for sr in source_roots)
        scored.append((0 if in_source_root else 1, -depth, str(path)))

    scored.sort()
    ordered: List[str] = []
    for _, _, path in scored:
        if path not in ordered:
            ordered.append(path)

    if not ordered:
        # Import errors have no useful frames: fall back to the module the test
        # failed to import, if we can name it.
        guess = _guess_from_import_error(failure, layout)
        if guess:
            ordered.append(guess)

    if not ordered and failure.test_file:
        # Plenty of real failures raise inside the test frame and never enter
        # the source at all -- `self.assertEqual(calc.multiply(3, 4), 14)`
        # fails after multiply returned perfectly well, so no source frame
        # exists. The code under test is then whatever this test file imports
        # from the project, which its own import statements state outright.
        ordered.extend(_from_test_imports(failure.test_file, layout))

    return ordered[:3]


def _from_test_imports(test_file: str, layout: ProjectLayout) -> List[str]:
    """Project modules the test file imports, in the order it imports them."""
    import ast

    try:
        tree = ast.parse(Path(test_file).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []

    modules: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)

    resolved: List[str] = []
    bases = [Path(p) for p in layout.import_roots] + [Path(layout.root)]
    for module in modules:
        relative = Path(*module.split("."))
        for base in bases:
            for candidate in (
                base / relative.with_suffix(".py"),
                base / relative / "__init__.py",
            ):
                if candidate.is_file():
                    path = str(candidate.resolve())
                    if path not in resolved and not _is_library(candidate):
                        resolved.append(path)
                    break
            else:
                continue
            break
    return resolved


def _guess_from_import_error(failure: TestFailure, layout: ProjectLayout) -> Optional[str]:
    """For ModuleNotFoundError, locate the module the test meant to import.

    This is inference, unlike everything else here, but it is inference over a
    name the traceback gives us explicitly ("No module named 'foo.bar'") rather
    than over a filename invented from a class name.
    """
    message = failure.exception_message or ""
    if "No module named" not in message and "cannot import name" not in message:
        return None

    import re

    match = re.search(r"No module named ['\"]([\w.]+)['\"]", message)
    if not match:
        match = re.search(r"cannot import name ['\"](\w+)['\"] from ['\"]([\w.]+)['\"]", message)
        if not match:
            return None
        module = match.group(2)
    else:
        module = match.group(1)

    relative = Path(*module.split("."))
    for base in [Path(p) for p in layout.import_roots] + [Path(layout.root)]:
        for candidate in (base / relative.with_suffix(".py"), base / relative / "__init__.py"):
            if candidate.is_file():
                return str(candidate.resolve())
    return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _absolute(raw: str, root: Path) -> Optional[Path]:
    if not raw:
        return None
    path = Path(raw)
    try:
        return path.resolve() if path.is_absolute() else (root / path).resolve()
    except (OSError, ValueError):
        return None


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_library(path: Path) -> bool:
    text = str(path).lower()
    return any(marker.lower() in text for marker in _LIBRARY_MARKERS)
