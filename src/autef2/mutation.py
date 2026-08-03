"""Mutation testing: change the source deliberately and see if the tests notice.

v1 drove cosmic-ray through a subprocess, configured for one module:

    APP_NAME = "InsuranceApp_Modified"
    target_module = f"source_files.{APP_NAME}"

Two things make that unusable on an uploaded project. The target is hardcoded,
and cosmic-ray (or mutmut) has to be installed into the project's environment
and driven through a session database whose CLI has changed across releases --
in an arbitrary project's virtualenv, with an arbitrary pinned pytest, that is a
compatibility problem with no upside.

So the mutations are generated here instead, from the AST, using position
information to rewrite exactly one operator or literal and leave the rest of the
file untouched. That has three advantages worth the code: it needs nothing
installed in the project environment, it is deterministic given a seed, and each
mutant carries the before-and-after line, so a surviving mutant can be described
to a model precisely rather than as a diff of a temporary file.

A mutation score only means something against a suite that passes. If the suite
is already failing, "the tests noticed" cannot be distinguished from "the tests
were already broken", so the caller is expected to check that first.
"""

from __future__ import annotations

import ast
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Mutant, ProjectLayout

logger = logging.getLogger(__name__)

#: Comparison flips. Each makes the opposite decision at a branch.
_COMPARISON_FLIPS: Dict[type, Tuple[str, str]] = {
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", ">="),
    ast.LtE: ("<=", ">"),
    ast.Gt: (">", "<="),
    ast.GtE: (">=", "<"),
    ast.Is: ("is", "is not"),
    ast.IsNot: ("is not", "is"),
    ast.In: ("in", "not in"),
    ast.NotIn: ("not in", "in"),
}

#: Arithmetic swaps. A test that only checks a result's type or truthiness will
#: not notice these; one that checks the value will.
_ARITHMETIC_SWAPS: Dict[type, Tuple[str, str]] = {
    ast.Add: ("+", "-"),
    ast.Sub: ("-", "+"),
    ast.Mult: ("*", "/"),
    ast.Div: ("/", "*"),
    ast.FloorDiv: ("//", "/"),
    ast.Mod: ("%", "//"),
    ast.Pow: ("**", "*"),
}

_BOOLEAN_SWAPS: Dict[type, Tuple[str, str]] = {
    ast.And: ("and", "or"),
    ast.Or: ("or", "and"),
}


@dataclass
class MutationSite:
    """One place the source can be changed, and what to change it to."""

    file: str
    lineno: int
    col_offset: int
    end_col_offset: int
    operator: str
    original_text: str
    replacement_text: str

    def key(self) -> tuple:
        return (self.file, self.lineno, self.col_offset, self.operator)


class Mutator:
    """Finds mutation sites and applies them one at a time."""

    def __init__(self, layout: ProjectLayout):
        self.layout = layout

    # -- planning ---------------------------------------------------------

    def sites(self, *, limit: Optional[int] = None, seed: int = 1337) -> List[MutationSite]:
        """Mutation sites across the project's source, sampled deterministically.

        Sampling rather than truncating: taking the first N would mutate one file
        exhaustively and never touch the rest, which reports a score for one file
        and calls it the project's.
        """
        found: List[MutationSite] = []
        for path in self._source_files():
            found.extend(self._sites_in(path))

        found.sort(key=lambda s: s.key())  # deterministic before sampling
        if limit is not None and len(found) > limit:
            rng = random.Random(seed)
            found = sorted(rng.sample(found, limit), key=lambda s: s.key())
        return found

    def _source_files(self) -> List[Path]:
        from .chunker import _source_files  # same definition of "source"

        return _source_files(self.layout)

    def _sites_in(self, path: Path) -> List[MutationSite]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            return []

        lines = text.splitlines()
        sites: List[MutationSite] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                sites.extend(self._compare_sites(node, path, lines))
            elif isinstance(node, ast.BinOp):
                site = self._operator_site(
                    node, path, lines, _ARITHMETIC_SWAPS, "arithmetic"
                )
                if site is not None:
                    sites.append(site)
            elif isinstance(node, ast.BoolOp):
                site = self._boolop_site(node, path, lines)
                if site is not None:
                    sites.append(site)
            elif isinstance(node, ast.Constant):
                site = self._constant_site(node, path, lines)
                if site is not None:
                    sites.append(site)

        return sites

    def _compare_sites(
        self, node: ast.Compare, path: Path, lines: Sequence[str]
    ) -> List[MutationSite]:
        sites: List[MutationSite] = []
        # The operator sits between the left operand and its comparator, and ast
        # gives no position for it, so it is located in the text between them.
        left = node.left
        for operator, comparator in zip(node.ops, node.comparators):
            flip = _COMPARISON_FLIPS.get(type(operator))
            if flip is None:
                left = comparator
                continue
            found = self._locate_between(lines, left, comparator, flip[0])
            if found is not None:
                lineno, col = found
                sites.append(
                    MutationSite(
                        file=str(path),
                        lineno=lineno,
                        col_offset=col,
                        end_col_offset=col + len(flip[0]),
                        operator=f"comparison {flip[0]} -> {flip[1]}",
                        original_text=flip[0],
                        replacement_text=flip[1],
                    )
                )
            left = comparator
        return sites

    def _operator_site(
        self,
        node: ast.BinOp,
        path: Path,
        lines: Sequence[str],
        table: Dict[type, Tuple[str, str]],
        label: str,
    ) -> Optional[MutationSite]:
        swap = table.get(type(node.op))
        if swap is None:
            return None
        found = self._locate_between(lines, node.left, node.right, swap[0])
        if found is None:
            return None
        lineno, col = found
        return MutationSite(
            file=str(path),
            lineno=lineno,
            col_offset=col,
            end_col_offset=col + len(swap[0]),
            operator=f"{label} {swap[0]} -> {swap[1]}",
            original_text=swap[0],
            replacement_text=swap[1],
        )

    def _boolop_site(
        self, node: ast.BoolOp, path: Path, lines: Sequence[str]
    ) -> Optional[MutationSite]:
        swap = _BOOLEAN_SWAPS.get(type(node.op))
        if swap is None or len(node.values) < 2:
            return None
        found = self._locate_between(lines, node.values[0], node.values[1], swap[0])
        if found is None:
            return None
        lineno, col = found
        return MutationSite(
            file=str(path),
            lineno=lineno,
            col_offset=col,
            end_col_offset=col + len(swap[0]),
            operator=f"boolean {swap[0]} -> {swap[1]}",
            original_text=swap[0],
            replacement_text=swap[1],
        )

    def _constant_site(
        self, node: ast.Constant, path: Path, lines: Sequence[str]
    ) -> Optional[MutationSite]:
        if node.lineno != getattr(node, "end_lineno", node.lineno):
            return None  # a multi-line string: leave it alone
        line = _line(lines, node.lineno)
        if line is None:
            return None
        original = line[node.col_offset : node.end_col_offset]

        value = node.value
        if isinstance(value, bool):
            replacement = "False" if value else "True"
        elif isinstance(value, int) and not isinstance(value, bool):
            replacement = str(value + 1)
        elif isinstance(value, float):
            replacement = repr(value + 1.0)
        else:
            return None  # strings and None are too often docstrings or sentinels

        if original.strip() != original or not original:
            return None  # position did not land cleanly on the literal
        return MutationSite(
            file=str(path),
            lineno=node.lineno,
            col_offset=node.col_offset,
            end_col_offset=node.end_col_offset,
            operator=f"literal {original} -> {replacement}",
            original_text=original,
            replacement_text=replacement,
        )

    def _locate_between(
        self, lines: Sequence[str], left: ast.AST, right: ast.AST, token: str
    ) -> Optional[Tuple[int, int]]:
        """Find ``token`` in the text between two operands, on one line.

        Operators have no position of their own in the AST. Searching only the
        span between the operands keeps the match honest: an ``and`` inside a
        string literal elsewhere on the line cannot be picked up.
        """
        left_end_line = getattr(left, "end_lineno", None)
        right_line = getattr(right, "lineno", None)
        if left_end_line is None or right_line is None or left_end_line != right_line:
            return None  # spans lines: not worth the ambiguity

        line = _line(lines, left_end_line)
        if line is None:
            return None
        start = getattr(left, "end_col_offset", 0)
        end = getattr(right, "col_offset", len(line))
        if start >= end:
            return None

        gap = line[start:end]
        index = gap.find(token)
        if index == -1:
            return None
        # A bare word operator must not match inside an identifier.
        if token.isalpha():
            absolute = start + index
            before = line[absolute - 1] if absolute > 0 else " "
            after_index = absolute + len(token)
            after = line[after_index] if after_index < len(line) else " "
            if before.isalnum() or before == "_" or after.isalnum() or after == "_":
                return None
        return left_end_line, start + index

    # -- applying ---------------------------------------------------------

    def apply(self, site: MutationSite) -> str:
        """Write the mutation and return the original file text."""
        path = Path(site.file)
        original = path.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines(keepends=True)

        index = site.lineno - 1
        if index >= len(lines):
            raise MutationError(f"{path.name} has no line {site.lineno}")

        line = lines[index]
        found = line[site.col_offset : site.end_col_offset]
        if found != site.original_text:
            raise MutationError(
                f"{path.name}:{site.lineno} holds {found!r}, expected "
                f"{site.original_text!r}"
            )

        lines[index] = (
            line[: site.col_offset]
            + site.replacement_text
            + line[site.end_col_offset :]
        )
        mutated = "".join(lines)

        # A mutation that does not parse is not a test of the suite.
        try:
            ast.parse(mutated)
        except SyntaxError as exc:
            raise MutationError(f"mutation would not parse: {exc}") from exc

        path.write_text(mutated, encoding="utf-8")
        return original

    def restore(self, site: MutationSite, original: str) -> None:
        Path(site.file).write_text(original, encoding="utf-8")

    def describe(self, site: MutationSite) -> Mutant:
        """The reportable form, carrying the changed line before and after."""
        line = ""
        try:
            lines = Path(site.file).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            line = _line(lines, site.lineno) or ""
        except OSError:
            pass

        mutated_line = (
            line[: site.col_offset]
            + site.replacement_text
            + line[site.end_col_offset :]
        )
        return Mutant(
            file=site.file,
            lineno=site.lineno,
            operator=site.operator,
            original=line.strip(),
            mutated=mutated_line.strip(),
        )


class MutationError(RuntimeError):
    """A mutation could not be applied. Never fatal: the site is skipped."""


def _line(lines: Sequence[str], lineno: int) -> Optional[str]:
    index = lineno - 1
    if 0 <= index < len(lines):
        return lines[index].rstrip("\n")
    return None
