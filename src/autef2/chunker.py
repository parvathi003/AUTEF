"""Split a source file into units a test can actually be written for.

v1's ``chunk_code`` cut the file every 512 characters:

    for line in lines:
        if len(current_chunk) + len(line) > max_chunk_size:
            chunks.append(...)

That measures the wrong thing twice -- it compares a line count against a
character budget -- and, more importantly, it splits wherever the budget runs
out. A function's signature lands in one chunk and its body in the next, and the
model is asked to write tests for half a function it cannot see the end of.

Here the file is split on syntax instead. Each top-level function and each class
becomes one unit, kept whole, and carries the module's imports with it so the
model can see what names are in scope. Nothing that cannot be tested in
isolation is offered: private helpers, ``if __name__`` blocks and re-export-only
modules are skipped rather than turned into a prompt that will produce a test
importing something that does not exist.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .models import ProjectLayout

logger = logging.getLogger(__name__)

#: Cap on the source text of one unit. A unit over this is truncated at a
#: statement boundary rather than split, so what the model sees is always
#: syntactically whole.
DEFAULT_MAX_UNIT_CHARS = 6_000


@dataclass
class CodeUnit:
    """One testable thing: a function, or a class with its methods."""

    name: str
    kind: str  # "function" | "class"
    source: str
    lineno: int
    #: Public methods, for a class. Used to tell the model what to cover.
    members: List[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}"


@dataclass
class ModuleUnits:
    """Everything worth testing in one module, plus the context to import it."""

    path: str
    import_name: str
    header: str = ""
    units: List[CodeUnit] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def testable(self) -> bool:
        return bool(self.units)

    def source_text(self, *, budget: int = DEFAULT_MAX_UNIT_CHARS * 2) -> str:
        """The units' source, joined, for a prompt. Header first."""
        parts = [self.header.strip()] if self.header.strip() else []
        remaining = budget - len(self.header)
        for unit in self.units:
            if remaining <= 0:
                break
            text = unit.source
            if len(text) > remaining:
                text = text[:remaining] + "\n    # ...[truncated]"
            parts.append(text)
            remaining -= len(text)
        return "\n\n".join(parts)


def module_import_name(path: str | Path, layout: ProjectLayout) -> str:
    """The dotted name the tests should import this module by.

    Derived from the import roots the runner puts on ``PYTHONPATH``, so the
    generated test imports the module the same way the project does. v1 instead
    told the model to walk ``sys.path`` upwards from a hardcoded ``source_files``
    directory, which only worked inside its own tree.
    """
    target = Path(path).resolve()
    roots = [Path(r).resolve() for r in layout.import_roots] + [Path(layout.root).resolve()]
    # Longest root first: with both the project and its src/ directory on the
    # path, src/ gives the shorter, correct dotted name.
    for root in sorted(roots, key=lambda p: len(p.parts), reverse=True):
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            return ".".join(parts)
    return target.stem


def split_module(
    path: str | Path,
    layout: ProjectLayout,
    *,
    max_unit_chars: int = DEFAULT_MAX_UNIT_CHARS,
) -> ModuleUnits:
    """Parse one module into testable units."""
    path = Path(path)
    module = ModuleUnits(path=str(path), import_name=module_import_name(path, layout))

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        module.skipped_reason = f"unreadable: {exc}"
        return module

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        module.skipped_reason = f"does not parse: {exc}"
        return module

    lines = text.splitlines()
    module.header = _header(tree, lines)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue  # private helper: not part of the module's contract
            module.units.append(_unit(node, lines, "function", max_unit_chars))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            unit = _unit(node, lines, "class", max_unit_chars)
            unit.members = [
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (not child.name.startswith("_") or child.name == "__init__")
            ]
            module.units.append(unit)

    if not module.units:
        module.skipped_reason = (
            "no public functions or classes to test"
            if tree.body
            else "empty module"
        )
    return module


def testable_modules(
    layout: ProjectLayout, *, limit: Optional[int] = None
) -> List[ModuleUnits]:
    """Every module in the project worth generating tests for, best first.

    Ordered by how much untested surface each one carries, so a cap on the
    number of modules spends the budget where it buys the most.
    """
    candidates = [
        ModuleUnits(path=str(p), import_name="")
        for p in _source_files(layout)
    ]
    modules: List[ModuleUnits] = []
    for candidate in candidates:
        module = split_module(candidate.path, layout)
        if module.testable:
            modules.append(module)
        else:
            logger.debug("Skipping %s: %s", candidate.path, module.skipped_reason)

    modules.sort(key=lambda m: len(m.units), reverse=True)
    return modules[:limit] if limit else modules


def modules_without_tests(
    layout: ProjectLayout, *, limit: Optional[int] = None
) -> List[ModuleUnits]:
    """Testable modules that no existing test file appears to cover.

    The match is by name -- ``test_operations.py`` for ``operations.py`` -- which
    is a convention, not a guarantee. It is used only to order work, never to
    decide that a module is untested; coverage measurement answers that
    properly.
    """
    existing = {Path(f).name for f in layout.test_files}
    modules = testable_modules(layout)

    def covered(module: ModuleUnits) -> bool:
        stem = Path(module.path).stem
        return f"test_{stem}.py" in existing or f"{stem}_test.py" in existing

    uncovered = [m for m in modules if not covered(m)]
    return uncovered[:limit] if limit else uncovered


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _source_files(layout: ProjectLayout) -> List[Path]:
    """Project modules that are source, not tests and not packaging."""
    from .ingest import EXCLUDED_DIRS, TEST_FILE_RE, VENV_DIR_NAMES

    roots = [Path(r) for r in (layout.source_roots or [layout.root])]

    # A test root that *is* a source root excludes nothing: a flat project with
    # no tests yet has test_roots == [project root], and treating that as "all
    # test code" would hide every module in the project from generation.
    source_resolved = {r.resolve() for r in roots}
    test_roots = [
        Path(r).resolve()
        for r in layout.test_roots
        if Path(r).resolve() not in source_resolved
    ]

    found: List[Path] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(
                part in EXCLUDED_DIRS or part in VENV_DIR_NAMES
                for part in path.parts
            ):
                continue
            if TEST_FILE_RE.match(path.name) or path.name == "conftest.py":
                continue
            if path.name in ("setup.py", "__init__.py"):
                continue
            resolved = path.resolve()
            if any(_within(resolved, tr) for tr in test_roots):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _header(tree: ast.Module, lines: Sequence[str]) -> str:
    """The module's imports and constants -- what a unit's code assumes exists."""
    kept: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            kept.append(_segment(node, lines))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.col_offset == 0:
            segment = _segment(node, lines)
            if len(segment) < 200:  # a constant, not a computed table
                kept.append(segment)
    return "\n".join(kept)


def _unit(node: ast.AST, lines: Sequence[str], kind: str, budget: int) -> CodeUnit:
    source = _segment(node, lines)
    truncated = False
    if len(source) > budget:
        source = _truncate_at_statement(source, budget)
        truncated = True
    return CodeUnit(
        name=getattr(node, "name", "?"),
        kind=kind,
        source=source,
        lineno=getattr(node, "lineno", 0),
        truncated=truncated,
    )


def _segment(node: ast.AST, lines: Sequence[str]) -> str:
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    # Decorators sit above lineno and are part of the definition.
    for decorator in getattr(node, "decorator_list", []) or []:
        start = min(start, getattr(decorator, "lineno", start + 1) - 1)
    return "\n".join(lines[start:end])


def _truncate_at_statement(source: str, budget: int) -> str:
    """Cut on a line boundary, never mid-expression."""
    kept: List[str] = []
    used = 0
    for line in source.splitlines():
        if used + len(line) > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append("    # ...[body truncated for length]")
    return "\n".join(kept)
