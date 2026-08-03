"""Assemble the evidence a repair prompt gets to see.

Context is the main cost lever: every rung of the ladder declares what it needs
(``include_project_tree``, ``include_sibling_tests``, ...) and nothing more is
sent. Rung 1 sees the source and the traceback; only a rung that has already
watched a cheaper repair fail pays for the wider view.

The project tree deserves a note. For an import failure the useful fact is not
the file listing but the *importable dotted names*, computed from the same
import roots the runner puts on PYTHONPATH -- so the model is told what the
test process can actually import, not what happens to be on disk.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

from .config import AutefConfig
from .models import ProjectLayout, Strategy, TestFailure
from .patcher import FunctionSpan

logger = logging.getLogger(__name__)

MAX_TREE_ENTRIES = 120
MAX_SIBLINGS = 3


class ContextBuilder:
    """Turns a failure plus a strategy into the user half of a prompt."""

    def __init__(self, layout: ProjectLayout, config: AutefConfig):
        self.layout = layout
        self.config = config

    def build(
        self,
        failure: TestFailure,
        strategy: Strategy,
        span: Optional[FunctionSpan],
        *,
        previous_attempts: Sequence[str] = (),
    ) -> str:
        blocks: List[str] = []

        blocks.append(self._failure_block(failure))

        if strategy.include_source and failure.source_files:
            blocks.append(self._source_block(failure.source_files))

        if strategy.scope == "file" or strategy.include_full_test_file:
            blocks.append(self._test_file_block(failure))
        elif span is not None:
            blocks.append(
                _fence("FAILING TEST FUNCTION", span.source, dedent=True)
            )

        if strategy.include_project_tree:
            blocks.append(self._tree_block())

        if strategy.include_sibling_tests and failure.test_file:
            sibling = self._siblings_block(failure)
            if sibling:
                blocks.append(sibling)

        if previous_attempts:
            blocks.append(
                "PREVIOUS REPAIR ATTEMPTS THAT DID NOT WORK (do not repeat them):\n"
                + "\n".join(f"- {a}" for a in previous_attempts)
            )

        blocks.append(self._task_block(failure, strategy))
        return "\n\n".join(b for b in blocks if b)

    def build_diagnostic(
        self, failure: TestFailure, span: Optional[FunctionSpan]
    ) -> str:
        """Evidence for the Failure Analysis Agent.

        Deliberately narrower than a repair prompt: the traceback, the failing
        function, and the source it touched. Diagnosis is asked on every
        failure, so its context is the one that has to stay cheap.
        """
        blocks = [self._failure_block(failure)]
        if span is not None:
            blocks.append(_fence("FAILING TEST FUNCTION", span.source, dedent=True))
        if failure.source_files:
            blocks.append(self._source_block(failure.source_files[:1]))
        return "\n\n".join(b for b in blocks if b)

    # -- blocks -----------------------------------------------------------

    def _failure_block(self, failure: TestFailure) -> str:
        lines = [
            "FAILING TEST",
            f"  test id     : {failure.nodeid}",
            f"  phase       : {failure.phase}",
            f"  exception   : {failure.exception_type or 'unknown'}",
            f"  message     : {_oneline(failure.exception_message, 400)}",
        ]
        if failure.test_file:
            lines.append(f"  test file   : {self._relative(failure.test_file)}")
        if failure.source_files:
            lines.append(
                "  source files: "
                + ", ".join(self._relative(p) for p in failure.source_files)
            )
        traceback = _truncate(failure.longrepr, self.config.max_traceback_chars)
        return "\n".join(lines) + "\n\n" + _fence("TRACEBACK", traceback)

    def _source_block(self, source_files: Sequence[str]) -> str:
        budget = self.config.max_source_chars
        parts: List[str] = []
        for path in source_files:
            if budget <= 0:
                break
            text = _read(path)
            if not text:
                continue
            share = _truncate(text, budget)
            budget -= len(share)
            parts.append(_fence(f"SOURCE: {self._relative(path)}", share))
        return "\n\n".join(parts)

    def _test_file_block(self, failure: TestFailure) -> str:
        if not failure.test_file:
            return ""
        text = _truncate(_read(failure.test_file), self.config.max_test_file_chars)
        return _fence(f"TEST FILE: {self._relative(failure.test_file)}", text)

    def _tree_block(self) -> str:
        modules = self._importable_modules()
        if not modules:
            return ""
        shown = modules[:MAX_TREE_ENTRIES]
        suffix = (
            f"\n... and {len(modules) - len(shown)} more"
            if len(modules) > len(shown)
            else ""
        )
        return _fence(
            "IMPORTABLE MODULES (dotted names the test process can import)",
            "\n".join(shown) + suffix,
        )

    def _importable_modules(self) -> List[str]:
        """Dotted names reachable from the runner's import roots."""
        names: List[str] = []
        seen = set()
        for root in self.layout.import_roots:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not d.startswith(".")
                    and d not in {"__pycache__", "node_modules", "build", "dist"}
                ]
                for filename in filenames:
                    if not filename.endswith(".py"):
                        continue
                    file_path = Path(dirpath) / filename
                    try:
                        relative = file_path.relative_to(root_path)
                    except ValueError:
                        continue
                    parts = list(relative.parts)
                    parts[-1] = parts[-1][:-3]
                    if parts[-1] == "__init__":
                        parts = parts[:-1]
                    if not parts:
                        continue
                    dotted = ".".join(parts)
                    if dotted not in seen:
                        seen.add(dotted)
                        names.append(f"{dotted}  ({self._relative(str(file_path))})")
        return sorted(names)

    def _siblings_block(self, failure: TestFailure) -> str:
        """A couple of passing tests from the same file, as a style reference."""
        text = _read(failure.test_file or "")
        if not text:
            return ""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return ""

        lines = text.splitlines()
        samples: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_") or node.name == failure.test_function:
                continue
            end = getattr(node, "end_lineno", node.lineno)
            body = "\n".join(lines[node.lineno - 1 : end])
            if len(body) < 1200:
                samples.append(body)
            if len(samples) >= MAX_SIBLINGS:
                break
        if not samples:
            return ""
        return _fence(
            "OTHER TESTS IN THIS FILE (conventions to follow)", "\n\n".join(samples)
        )

    def _task_block(self, failure: TestFailure, strategy: Strategy) -> str:
        if strategy.scope == "file":
            return (
                f"TASK: {strategy.label}. Return the complete corrected test "
                f"file so that {failure.nodeid} passes."
            )
        return (
            f"TASK: {strategy.label}. Return the corrected "
            f"`{failure.test_function}` function so that {failure.nodeid} passes."
        )

    def _relative(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path(self.layout.root).resolve()))
        except (ValueError, OSError):
            return path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n\n...[{len(text) - limit} characters omitted]...\n\n{text[-tail:]}"


def _oneline(text: str, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[:limit]


def _fence(title: str, body: str, *, dedent: bool = False) -> str:
    if dedent:
        import textwrap

        body = textwrap.dedent(body)
    return f"--- {title} ---\n{body}\n--- end ---"
