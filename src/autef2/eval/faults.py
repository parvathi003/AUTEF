"""Seed known faults into a project's tests, to get a controlled sample.

Real open-source projects mostly ship green suites, so an evaluation that waits
for naturally occurring failures gets a tiny and badly skewed sample. Seeding
faults fixes three problems at once: the sample is large enough to say
something, it is stratified across the failure taxonomy by construction, and
the ground truth is known -- we can tell a real repair from a lucky one because
we know exactly what was broken.

The five injectors correspond to five root causes the repair ladders address:

    broken_import      -> import_error
    wrong_expected     -> assertion_mismatch
    wrong_patch_target -> mock_misconfiguration
    bad_signature      -> api_misuse
    broken_setup       -> fixture_setup_error

This complements natural failures rather than replacing them. Seeded faults are
by construction the kind of thing a repair loop is good at; report them
separately from naturally failing tests and do not average the two together.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..models import ProjectLayout, RootCause

logger = logging.getLogger(__name__)


@dataclass
class FaultRecord:
    """One seeded fault, with enough detail to score a repair against it."""

    kind: str
    expected_cause: str
    file: str
    lineno: int
    original_line: str
    mutated_line: str
    enclosing_test: Optional[str] = None
    description: str = ""

    def to_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


@dataclass
class _Site:
    """A place in a test file where a given fault can be seeded."""

    kind: str
    lineno: int
    enclosing_test: Optional[str]
    apply: object  # Callable[[str], Optional[str]]
    description: str


FAULT_CAUSES = {
    "broken_import": RootCause.IMPORT_ERROR.value,
    "wrong_expected": RootCause.ASSERTION_MISMATCH.value,
    "wrong_patch_target": RootCause.MOCK_MISCONFIGURATION.value,
    "bad_signature": RootCause.API_MISUSE.value,
    "broken_setup": RootCause.FIXTURE_SETUP_ERROR.value,
}

ALL_KINDS = tuple(FAULT_CAUSES)

#: Faults that take out every test in the file rather than one test.
#:
#: A broken module-level import turns the whole file into a single collection
#: error, so seeding one alongside three other faults does not give four
#: observations -- it gives one, and silently destroys the other three. These
#: kinds therefore get a file to themselves.
FILE_SCOPED_KINDS = frozenset({"broken_import", "broken_setup"})

MARKER = "AUTEF_INJECTED_FAULT"


class FaultInjector:
    """Seeds faults into a project's test files, deterministically."""

    def __init__(
        self,
        layout: ProjectLayout,
        *,
        seed: int = 1337,
        kinds: Sequence[str] = ALL_KINDS,
        max_per_file: int = 2,
        passing_tests: Optional[Sequence[str]] = None,
    ):
        self.layout = layout
        self.random = random.Random(seed)
        self.kinds = [k for k in kinds if k in FAULT_CAUSES]
        self.max_per_file = max_per_file
        # Seeding a fault into an already-failing test produces a confounded
        # observation: you cannot tell whether a repair addressed the seeded
        # fault or the pre-existing one. Pass the suite's passing tests to
        # restrict seeding to them.
        self.passing_names = (
            {t.split("::")[-1].split("[")[0] for t in passing_tests}
            if passing_tests is not None
            else None
        )
        #: Why the last ``inject`` seeded fewer faults than asked for, if it
        #: did. A comparison run on a quarter of the faults it declared is not
        #: the experiment its protocol describes, so the shortfall has to reach
        #: the report rather than an INFO log.
        self.shortfall: Optional[str] = None

    def inject(self, count: int) -> List[FaultRecord]:
        """Seed up to ``count`` faults, spread evenly across the kinds.

        Per-test kinds are placed first and file-scoped kinds only into files
        that nothing else claimed. The order matters more than it looks: a
        file-scoped fault turns its whole file into one collection error, so
        when it goes first it swallows every other fault in that file. On a
        project with one test file that left a single import error -- the one
        failure v1 cannot attempt at all, having no test function to replace --
        and the v1-versus-v2 gap was then decided by seeding order rather than
        by either repair loop.
        """
        self.shortfall = None
        candidates: Dict[str, List[tuple]] = {kind: [] for kind in self.kinds}

        for test_file in self.layout.test_files:
            for site in self._sites(test_file):
                if site.kind not in candidates:
                    continue
                if not self._is_eligible(site):
                    continue
                candidates[site.kind].append((test_file, site))

        for sites in candidates.values():
            self.random.shuffle(sites)

        per_test_kinds = [k for k in self.kinds if k not in FILE_SCOPED_KINDS]
        file_scoped_kinds = [k for k in self.kinds if k in FILE_SCOPED_KINDS]

        selected: List[tuple] = []
        per_file: Dict[str, int] = {}
        claimed_whole_file: set = set()

        # File-scoped kinds keep their share of the budget rather than taking
        # whatever is left: they map to import and fixture failures, which are
        # a real and interesting part of the mix.
        file_scoped_budget = (
            round(count * len(file_scoped_kinds) / len(self.kinds))
            if self.kinds
            else 0
        )
        per_test_target = count - file_scoped_budget

        def take(kinds: Sequence[str], limit: int) -> None:
            exhausted: set = set()
            while len(selected) < count and len(exhausted) < len(kinds):
                for kind in kinds:
                    if len(selected) >= limit or len(selected) >= count:
                        return
                    queue = candidates[kind]
                    if not queue:
                        exhausted.add(kind)
                        continue
                    test_file, site = queue.pop()

                    if test_file in claimed_whole_file:
                        continue
                    if site.kind in FILE_SCOPED_KINDS:
                        # Only a file nothing else is using: sharing would
                        # destroy the other faults without saying so.
                        if per_file.get(test_file, 0):
                            continue
                        claimed_whole_file.add(test_file)
                    elif per_file.get(test_file, 0) >= self.max_per_file:
                        continue

                    per_file[test_file] = per_file.get(test_file, 0) + 1
                    selected.append((test_file, site))

        if per_test_kinds:
            take(per_test_kinds, per_test_target)
        if file_scoped_kinds:
            take(file_scoped_kinds, count)
        # Anything the file-scoped budget could not place goes back to per-test
        # kinds, so a project with one test file still gets its faults.
        if per_test_kinds and len(selected) < count:
            take(per_test_kinds, count)

        records: List[FaultRecord] = []
        # Apply bottom-up within each file so earlier edits cannot shift the
        # line numbers of later ones.
        by_file: Dict[str, List[_Site]] = {}
        for test_file, site in selected:
            by_file.setdefault(test_file, []).append(site)

        for test_file, sites in by_file.items():
            path = Path(test_file)
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            applied: List[FaultRecord] = []
            for site in sorted(sites, key=lambda s: s.lineno, reverse=True):
                index = site.lineno - 1
                if not (0 <= index < len(lines)):
                    continue
                original = lines[index]
                mutated = site.apply(original)  # type: ignore[operator]
                if not mutated or mutated == original:
                    continue
                lines[index] = mutated + f"  # {MARKER}: {site.kind}"
                applied.append(
                    FaultRecord(
                        kind=site.kind,
                        expected_cause=FAULT_CAUSES[site.kind],
                        file=str(path),
                        lineno=site.lineno,
                        original_line=original.strip(),
                        mutated_line=mutated.strip(),
                        enclosing_test=site.enclosing_test,
                        description=site.description,
                    )
                )
            if applied:
                try:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    records.extend(applied)
                except OSError as exc:
                    logger.warning("Could not write seeded faults to %s: %s", path, exc)

        # Measured on what reached disk, not on what was selected: a site can
        # still be dropped at apply time when the rewrite would be a no-op.
        if len(records) < count:
            self.shortfall = (
                f"seeded {len(records)} of {count} requested faults: "
                f"{len(self.layout.test_files)} test file(s) offered no more "
                "eligible sites. Faults go only into currently-passing tests, "
                "and a file given an import or setup fault can hold no others."
            )
            logger.info(self.shortfall)

        logger.info(
            "Seeded %d faults across %d files", len(records), len(set(r.file for r in records))
        )
        return records

    def _is_eligible(self, site: _Site) -> bool:
        """Only seed into tests that currently pass, when we know which do."""
        if self.passing_names is None:
            return True
        if site.kind in FILE_SCOPED_KINDS:
            return True  # affects the file, not one named test
        return site.enclosing_test in self.passing_names

    # -- site discovery ---------------------------------------------------

    def _sites(self, test_file: str) -> List[_Site]:
        try:
            text = Path(test_file).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            return []

        enclosing = _enclosing_tests(tree)
        sites: List[_Site] = []

        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", None)
            if lineno is None:
                continue
            test_name = enclosing.get(lineno)

            if isinstance(node, (ast.Import, ast.ImportFrom)) and test_name is None:
                sites.append(
                    _Site(
                        "broken_import", lineno, None, _break_import,
                        "module renamed to one that does not exist",
                    )
                )

            elif isinstance(node, ast.Call):
                name = _call_name(node.func) or ""
                short = name.split(".")[-1]

                if short in ("patch", "patch_object") and node.args:
                    if isinstance(node.args[0], ast.Constant) and isinstance(
                        node.args[0].value, str
                    ):
                        sites.append(
                            _Site(
                                "wrong_patch_target", lineno, test_name,
                                _break_patch_target,
                                "patch target points at the definition site "
                                "instead of the lookup site",
                            )
                        )

                elif short in ("assertEqual", "assertNotEqual", "assertAlmostEqual"):
                    if test_name and _has_literal(node.args):
                        sites.append(
                            _Site(
                                "wrong_expected", lineno, test_name,
                                _break_expected,
                                "expected literal changed to a wrong value",
                            )
                        )

                elif test_name and short and not short.startswith("assert") and node.args:
                    sites.append(
                        _Site(
                            "bad_signature", lineno, test_name, _break_signature,
                            "an argument the callee does not accept was added",
                        )
                    )

            elif isinstance(node, ast.Assert) and test_name:
                if isinstance(node.test, ast.Compare) and _has_literal(
                    node.test.comparators
                ):
                    sites.append(
                        _Site(
                            "wrong_expected", lineno, test_name, _break_expected,
                            "expected literal changed to a wrong value",
                        )
                    )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("setUp", "setup_method") and node.body:
                    first = node.body[0]
                    sites.append(
                        _Site(
                            "broken_setup", first.lineno, None, _break_setup,
                            "setup constructs the object with a wrong argument",
                        )
                    )

        return sites


# ---------------------------------------------------------------------------
# individual mutations, applied to a single source line
# ---------------------------------------------------------------------------


def _break_import(line: str) -> Optional[str]:
    match = re.match(r"^(\s*)(from|import)\s+([\w.]+)(.*)$", line)
    if not match:
        return None
    indent, keyword, module, rest = match.groups()
    parts = module.split(".")
    parts[-1] = parts[-1] + "_missing"
    return f"{indent}{keyword} {'.'.join(parts)}{rest}"


def _break_patch_target(line: str) -> Optional[str]:
    match = re.search(r"""(['"])([\w.]+\.[\w]+)\1""", line)
    if not match:
        return None
    quote, target = match.group(1), match.group(2)
    parts = target.split(".")
    # Move the patch to the definition module: the classic wrong target.
    broken = f"{parts[-2]}.{parts[-1]}" if len(parts) > 2 else f"builtins.{parts[-1]}"
    return line[: match.start()] + f"{quote}{broken}{quote}" + line[match.end():]


def _break_expected(line: str) -> Optional[str]:
    number = re.search(r"(?<![\w.])(-?\d+)(?![\w.])", line)
    if number:
        value = int(number.group(1))
        return line[: number.start()] + str(value + 7) + line[number.end():]
    text = re.search(r"""(['"])((?:(?!\1).){1,60})\1""", line)
    if text:
        quote, content = text.group(1), text.group(2)
        return (
            line[: text.start()] + f"{quote}{content}_wrong{quote}" + line[text.end():]
        )
    return None


def _break_signature(line: str) -> Optional[str]:
    match = re.search(r"\(([^()]*)\)", line)
    if not match:
        return None
    inner = match.group(1).strip()
    if not inner or "=" in inner.split(",")[-1]:
        return None
    return line[: match.end() - 1] + ", 999" + line[match.end() - 1:]


def _break_setup(line: str) -> Optional[str]:
    match = re.search(r"\(([^()]*)\)", line)
    if not match:
        return None
    return line[: match.end() - 1] + ", unexpected_arg=True" + line[match.end() - 1:]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _enclosing_tests(tree: ast.AST) -> Dict[int, str]:
    """Map every line inside a test function to that function's name."""
    mapping: Dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            end = getattr(node, "end_lineno", node.lineno)
            for lineno in range(node.lineno, end + 1):
                mapping[lineno] = node.name
    return mapping


def _call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _has_literal(nodes: Sequence[ast.expr]) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str))
        for n in nodes
    )
