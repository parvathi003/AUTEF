"""Locate and replace test functions using the AST, and roll back cleanly.

v1 did this with a regex over the file text and, when the fix came back,
deleted the matched span with ``content.replace(fn, "")`` and appended the new
function to the end of the file. On a method inside a ``unittest.TestCase``
that appends at module level, silently moving the test out of its class -- it
then never runs again and the suite looks greener than it is.

Here the span is taken from the AST (decorators included), the replacement is
re-indented to the original nesting level, and both the replacement fragment
and the resulting whole file must parse before anything is written. A rejected
patch costs nothing: it never reaches disk and never burns a test re-run.
"""

from __future__ import annotations

import ast
import logging
import re
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PatchError(RuntimeError):
    pass


@dataclass
class FunctionSpan:
    """A function's exact extent in a file, decorators included."""

    path: str
    name: str
    class_name: Optional[str]
    start_line: int  # 1-based, inclusive
    end_line: int    # 1-based, inclusive
    indent: str
    source: str

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


def find_function(
    path: str, function_name: str, class_name: Optional[str] = None
) -> Optional[FunctionSpan]:
    """Find ``function_name`` (optionally inside ``class_name``) in ``path``."""
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError) as exc:
        logger.debug("Cannot parse %s: %s", path, exc)
        return None

    lines = text.splitlines()
    node = _find_node(tree, function_name, class_name)
    if node is None:
        return None

    # lineno points at `def`; decorators sit above it and belong to the span.
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    end = getattr(node, "end_lineno", None) or _infer_end(lines, node.lineno)

    body = "\n".join(lines[start - 1 : end])
    indent = re.match(r"[ \t]*", lines[start - 1]).group(0)

    return FunctionSpan(
        path=str(file_path),
        name=function_name,
        class_name=class_name,
        start_line=start,
        end_line=end,
        indent=indent,
        source=body,
    )


def _find_node(
    tree: ast.AST, function_name: str, class_name: Optional[str]
) -> Optional[ast.AST]:
    """Prefer the requested class, but fall back to a unique global match."""
    if class_name:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if _is_function(child) and child.name == function_name:
                        return child

    matches = [
        node
        for node in ast.walk(tree)
        if _is_function(node) and node.name == function_name
    ]
    if len(matches) == 1:
        return matches[0]
    if matches and class_name is None:
        # Ambiguous without a class to disambiguate; refuse rather than guess.
        logger.debug(
            "%d functions named %s and no class given", len(matches), function_name
        )
    return None


def _is_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _infer_end(lines: List[str], start_lineno: int) -> int:
    """Fallback for interpreters without ``end_lineno`` (pre-3.8)."""
    base_indent = len(lines[start_lineno - 1]) - len(lines[start_lineno - 1].lstrip())
    for index in range(start_lineno, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            return index
    return len(lines)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def replace_function(span: FunctionSpan, new_code: str) -> str:
    """Splice ``new_code`` over ``span``. Returns the new file text.

    Raises PatchError -- without touching the file -- if the replacement is not
    a single function, does not parse, or breaks the file.
    """
    fragment = normalise_fragment(new_code)
    if not fragment:
        raise PatchError("empty replacement")

    _validate_fragment(fragment, span.name)

    indented = textwrap.indent(fragment, span.indent)
    path = Path(span.path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    updated = lines[: span.start_line - 1] + indented.splitlines() + lines[span.end_line :]
    text = "\n".join(updated) + "\n"

    try:
        ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"patched file would not parse: {exc}") from exc

    path.write_text(text, encoding="utf-8")
    return text


def replace_file(path: str, new_code: str) -> str:
    """Replace a whole test file, after checking it parses."""
    text = strip_code_fences(new_code).strip() + "\n"
    if not text.strip():
        raise PatchError("empty replacement file")
    try:
        ast.parse(text)
    except SyntaxError as exc:
        raise PatchError(f"replacement file would not parse: {exc}") from exc
    Path(path).write_text(text, encoding="utf-8")
    return text


def _validate_fragment(fragment: str, expected_name: str) -> None:
    try:
        tree = ast.parse(fragment)
    except SyntaxError as exc:
        raise PatchError(f"replacement does not parse: {exc}") from exc

    functions = [n for n in tree.body if _is_function(n)]
    if len(tree.body) != len(functions) or not functions:
        raise PatchError(
            "replacement must be one or more bare function definitions "
            "(no imports, no class headers)"
        )
    if not any(f.name == expected_name for f in functions):
        # Allowing a rename would orphan the failing nodeid, so we would have
        # no way to verify the repair actually fixed the test we asked about.
        raise PatchError(
            f"replacement does not define {expected_name!r} "
            f"(found {[f.name for f in functions]})"
        )


def normalise_fragment(code: str) -> str:
    """Clean up what the model returned into a bare, dedented function."""
    text = strip_code_fences(code)
    # Drop leading prose the model sometimes prepends despite instructions.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("def ", "async def ", "@")):
            lines = lines[index:]
            break
    text = "\n".join(lines)
    return textwrap.dedent(text).strip("\n")


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n?|\n?```\s*$")


def strip_code_fences(text: str) -> str:
    """Remove markdown fences. Models add them regardless of instructions."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        blocks = re.findall(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", cleaned, re.DOTALL)
        if blocks:
            return max(blocks, key=len).strip("\n")
        cleaned = _FENCE_RE.sub("", cleaned)
    return cleaned.strip("\n")


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


class Snapshot:
    """Remembers file contents so a failed repair can be undone exactly.

    Escalation only means anything if each rung starts from the same state; a
    rung that leaves a half-applied edit behind would let the next rung 'fix' a
    problem the previous one caused.
    """

    def __init__(self) -> None:
        self._contents: Dict[str, str] = {}

    def capture(self, *paths: str) -> None:
        for path in paths:
            if not path or path in self._contents:
                continue
            try:
                self._contents[path] = Path(path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue

    def restore(self, *paths: str) -> None:
        targets = paths or tuple(self._contents)
        for path in targets:
            content = self._contents.get(path)
            if content is None:
                continue
            try:
                Path(path).write_text(content, encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not restore %s: %s", path, exc)

    def original(self, path: str) -> Optional[str]:
        return self._contents.get(path)

    def __contains__(self, path: str) -> bool:
        return path in self._contents


def copy_tree(src: str, dst: str) -> None:
    """Full-project snapshot, used by the benchmark to reset between arms."""
    destination = Path(dst)
    if destination.exists():
        shutil.rmtree(destination, onerror=_force_remove)
    shutil.copytree(
        src,
        destination,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", ".git", "*.pyc", ".venv", "venv"
        ),
    )


def _force_remove(func, path, _exc_info):
    import os

    os.chmod(path, 0o700)
    func(path)
