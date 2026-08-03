"""Test Generation Agent: write a suite for a module that has none.

This is v1's capability, kept and re-pointed. v1's ``get_prompt`` asked for a
comprehensive, edge-case-focused suite, and that instruction is preserved. What
changes is everything around it:

* **the import.** v1 told the model to reconstruct ``sys.path`` at runtime by
  string-replacing ``tests`` with ``source_files`` in ``__file__``. That is a
  fact about one repository. Here the module's real dotted name is derived from
  the project's import roots and given to the model, and the runner already puts
  those roots on ``PYTHONPATH``.
* **what gets sent.** Whole functions and classes (see ``chunker``), not a
  512-character slice that may end mid-body.
* **what happens next.** A generated file is provisional. It has to parse, be
  collected by pytest, and contain tests that actually run before it is kept;
  otherwise it is reverted. v1 wrote whatever came back and moved on, so a file
  that could not even be imported still counted as tests generated.

Generated tests that *fail* are not a failure of this agent -- they are the
input to the repair loop, which is the point of running the two together.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Sequence

from ..chunker import ModuleUnits
from ..config import AutefConfig
from ..llm import LLMClient, LLMError
from ..models import GeneratedTest, ProjectLayout
from ..patcher import strip_code_fences

logger = logging.getLogger(__name__)

#: Written into every generated file, so a later run can tell what it authored.
GENERATED_MARKER = "AUTEF_GENERATED"


def authored_here(path: Path) -> bool:
    """Did this framework write the file at ``path``?"""
    try:
        return path.is_file() and GENERATED_MARKER in path.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:  # pragma: no cover - unreadable counts as "not ours"
        return False


def reserve_path(preferred: Path) -> Path:
    """A path that is safe to write a generated test to.

    The preferred name is used when it is free, and when it holds a file this
    framework wrote before -- re-running a phase should replace its own output
    rather than accumulate copies of it. Any other existing file is left alone
    and a suffixed name is taken instead.

    This is not a nicety. ``test_<module>.py`` is exactly what a project calls
    its own test file, and generation used to write straight over it: the
    project's tests were destroyed, and because the replacements were written
    against observed behaviour they all passed, so the run reported a suite
    with nothing failing. Silently deleting the user's tests and calling it
    success is the worst outcome this framework can produce.

    Terminates: every iteration either returns or names a distinct existing
    file, and there are finitely many.
    """
    if not preferred.exists() or authored_here(preferred):
        return preferred

    parent, stem, suffix = preferred.parent, preferred.stem, preferred.suffix
    index = 1
    while True:
        candidate = parent / f"{stem}_autef{index if index > 1 else ''}{suffix}"
        if not candidate.exists() or authored_here(candidate):
            return candidate
        index += 1

SYSTEM_PROMPT = (
    "You are a Python test engineer. You write pytest-compatible unit tests "
    "that are specific, deterministic, and free of external dependencies. You "
    "return only Python code."
)


def build_generation_prompt(module: ModuleUnits, layout: ProjectLayout) -> str:
    """v1's instruction, with the import made real and the framework corrected.

    v1 asked for ``unittest`` and for a ``sys.path`` prelude. pytest collects
    ``unittest.TestCase`` perfectly well, so the framework choice is left open,
    but the path prelude is dropped: it encoded one project's directory layout
    into every generated file.
    """
    inventory = "\n".join(
        f"- {unit.label}"
        + (f" (methods: {', '.join(unit.members)})" if unit.members else "")
        for unit in module.units
    )

    return f"""Write a unit test suite for this Python module.

Module: {Path(module.path).name}
Import it as: `{module.import_name}`

What to cover:
{inventory}

Source:
```python
{module.source_text()}
```

Requirements:
- Import the code under test from `{module.import_name}`. That import works as
  written; do not add sys.path manipulation, and do not guess a different path.
- Use plain pytest test functions named `test_*`, or unittest.TestCase classes.
  Either is collected.
- Test real behaviour visible in the source above: return values, edge cases,
  boundary conditions, and exceptions that the code raises explicitly.
- Assert on specific values. `assert result == 12` is a test; `assert result`
  and `assert True` are not.
- Do not test anything the source does not do. If you are unsure what a
  function returns, do not invent an expectation for it -- write a test for
  something the source makes certain.
- No network, no database, no file system writes outside pytest's tmp_path, and
  no sleeping. Use unittest.mock for collaborators the module imports.
- Do not modify or import the project's other test files.
- Return only Python code, no markdown fences and no commentary.
"""


class TestGenerationAgent:
    """Writes a test file for a module, and keeps it only if it holds up."""

    #: Stops pytest collecting this class when it scans AUTEF's own tree.
    __test__ = False

    def __init__(
        self,
        llm: Optional[LLMClient],
        config: AutefConfig,
        layout: ProjectLayout,
    ):
        self.llm = llm
        self.config = config
        self.layout = layout

    # -- public API -------------------------------------------------------

    def generate(self, module: ModuleUnits) -> GeneratedTest:
        """Generate, write and validate one test file."""
        started = time.time()
        record = GeneratedTest(
            module=module.path,
            module_import=module.import_name,
            units=[unit.label for unit in module.units],
        )

        if self.llm is None:
            record.error = "no model configured"
            return record

        scoped = self.llm.scoped()
        try:
            reply = scoped.complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_generation_prompt(module, self.layout)},
                ],
                max_tokens=self.config.max_output_tokens,
            )
        except LLMError as exc:
            self._bill(record, scoped, started)
            record.error = f"model call failed: {exc}"
            return record
        self._bill(record, scoped, started)

        code = strip_code_fences(reply)
        if not code.strip():
            record.error = "model returned nothing"
            return record

        problem = validate_generated(code, module.import_name)
        if problem is not None:
            record.error = problem
            return record

        destination = self.destination_for(module)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(_with_header(code, module), encoding="utf-8")
        except OSError as exc:
            record.error = f"could not write {destination}: {exc}"
            return record

        record.test_file = str(destination)
        record.accepted = True
        record.duration_s = time.time() - started
        logger.info(
            "  generated %s for %s (%d unit(s))",
            destination.name, module.import_name, len(module.units),
        )
        return record

    def destination_for(self, module: ModuleUnits) -> Path:
        """Where a generated file goes.

        Into the project's own test root when it has one, so the generated tests
        sit with the existing suite and are collected by the same run. A project
        with no tests at all gets a ``tests`` directory created for it.

        The name is reserved rather than assumed: this phase generates for
        modules that already have a test file whenever no untested module is
        left, and ``test_<module>.py`` is that file's name.
        """
        stem = Path(module.path).stem
        root = self.test_root()
        return reserve_path(root / f"test_{stem}.py")

    def test_root(self) -> Path:
        if self.layout.test_roots:
            return Path(self.layout.test_roots[0])
        return Path(self.layout.root) / "tests"

    def revert(self, record: GeneratedTest) -> None:
        """Remove a generated file that did not survive validation."""
        if not record.test_file:
            return
        path = Path(record.test_file)
        try:
            if authored_here(path):
                path.unlink()
                logger.info("  reverted %s", path.name)
        except OSError as exc:  # pragma: no cover - unlikely, never fatal
            logger.warning("Could not remove %s: %s", path, exc)
        record.accepted = False

    # -- helpers ----------------------------------------------------------

    def _bill(self, record: GeneratedTest, scoped: LLMClient, started: float) -> None:
        record.prompt_tokens = scoped.usage.prompt_tokens
        record.completion_tokens = scoped.usage.completion_tokens
        record.cost_usd = scoped.usage.cost_usd
        record.duration_s = time.time() - started


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate_generated(code: str, import_name: str) -> Optional[str]:
    """Reject generated code before it is written, with the reason, or None.

    Cheap static checks only. Whether the tests pass is decided by running them,
    not here -- a generated test that fails is work for the repair loop, not a
    reason to throw the file away. Shared by every agent that writes tests, so
    the standard for "this is a test file at all" is one standard.
    """
    import ast

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"generated code does not parse: {exc}"

    has_test = any(
        (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
        )
        or (
            isinstance(node, ast.ClassDef)
            and (
                node.name.startswith("Test")
                or any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test")
                    for child in node.body
                )
            )
        )
        for node in ast.walk(tree)
    )
    if not has_test:
        return "generated code contains no test functions"

    if not _imports_module(tree, import_name):
        return (
            f"generated code does not import {import_name}, so it does "
            "not test this module"
        )
    return None


def _imports_module(tree, import_name: str) -> bool:
    """Did the model import the module we asked it to test?

    Accepts any import that reaches the target -- the module itself, its parent
    package, or a name from it -- because ``from calc.operations import X`` and
    ``import calc.operations`` are both correct.
    """
    import ast

    wanted = import_name.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _prefix_match(alias.name.split("."), wanted):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _prefix_match(node.module.split("."), wanted):
                return True
    return False


def _prefix_match(candidate: Sequence[str], wanted: Sequence[str]) -> bool:
    shared = min(len(candidate), len(wanted))
    return shared > 0 and list(candidate[:shared]) == list(wanted[:shared])


def _with_header(code: str, module: ModuleUnits) -> str:
    return (
        f'"""Tests for {module.import_name}.\n\n'
        f"Written by AUTEF ({GENERATED_MARKER}). Reviewed by no one: treat as a\n"
        f'starting point, not as a specification.\n"""\n\n'
        + code.strip()
        + "\n"
    )
