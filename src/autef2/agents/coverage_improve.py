"""Coverage Agent: write tests for the lines and branches nothing reached.

v1's version of this worked, and its prompt is preserved in intent: show the
model the source, the existing tests, the missing statements and the missing
branches, and ask for additional tests aimed at them.

Two things are different. The missing lines and branches come from a coverage
run in the project's own environment rather than from a host-process
``coverage`` object pointed at ``*/source_files/*``. And the new tests are
written to their own file rather than spliced into the project's existing test
file: appending to a file that the user wrote risks corrupting it for a gain
that a separate file achieves just as well, and it keeps the provenance of
generated tests obvious.

What is *not* claimed here is that coverage went up. That is measured afterwards
by running coverage again, because "the model produced tests aimed at line 47"
and "line 47 is now covered" are different statements.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional

from ..config import AutefConfig
from ..llm import LLMClient, LLMError
from ..models import FileCoverage, GeneratedTest, ProjectLayout
from ..patcher import strip_code_fences
from .generation import GENERATED_MARKER, reserve_path, validate_generated

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Python test engineer. Given a module and the exact lines and "
    "branches its test suite never reaches, you write additional pytest tests "
    "that execute those paths. You return only Python code."
)


def build_coverage_prompt(
    file_coverage: FileCoverage,
    source: str,
    import_name: str,
    existing_tests: str,
) -> str:
    """v1's coverage prompt, with the gap description made precise."""
    missing_lines = ", ".join(str(n) for n in file_coverage.missing_lines[:60]) or "none"
    missing_branches = (
        ", ".join(
            f"line {a} never goes to {'exit' if b < 0 else f'line {b}'}"
            for a, b in file_coverage.missing_branches[:30]
        )
        or "none"
    )

    numbered = "\n".join(
        f"{index:4} | {line}"
        for index, line in enumerate(source.splitlines(), start=1)
    )

    existing = existing_tests.strip()
    existing_section = (
        f"""
Tests that already exist for this module (do not repeat them):
```python
{existing[:4000]}
```
"""
        if existing
        else "\nThis module has no tests yet.\n"
    )

    return f"""This module is tested, but some of it is never executed.

Import it as: `{import_name}`
Line coverage: {file_coverage.line_rate:.0%}    Branch coverage: {file_coverage.branch_rate:.0%}

Statements never executed: {missing_lines}
Branches never taken: {missing_branches}

Source, with line numbers:
```python
{numbered}
```
{existing_section}
Write additional tests that execute the listed lines and branches.

Requirements:
- Import from `{import_name}`. That import works as written; add no sys.path code.
- Each test must reach at least one of the listed lines or branches. Work out
  what input gets there by reading the source above.
- Assert on specific observable behaviour. A test that reaches a line but
  asserts nothing about it raises coverage while testing nothing, which is the
  failure mode this whole exercise is supposed to avoid.
- Do not restate the tests that already exist.
- No network, no database, no sleeping. Use unittest.mock for collaborators.
- Return only Python code, no markdown fences and no commentary.
"""


class CoverageImprovementAgent:
    """Writes tests aimed at specific uncovered lines and branches."""

    def __init__(
        self,
        llm: Optional[LLMClient],
        config: AutefConfig,
        layout: ProjectLayout,
    ):
        self.llm = llm
        self.config = config
        self.layout = layout

    def generate(
        self,
        file_coverage: FileCoverage,
        *,
        import_name: str,
        existing_tests: str = "",
        destination: Optional[Path] = None,
    ) -> GeneratedTest:
        started = time.time()
        record = GeneratedTest(
            module=file_coverage.path,
            module_import=import_name,
            units=[
                f"{len(file_coverage.missing_lines)} missing line(s)",
                f"{len(file_coverage.missing_branches)} missing branch(es)",
            ],
        )

        if self.llm is None:
            record.error = "no model configured"
            return record

        try:
            source = Path(file_coverage.path).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            record.error = f"source unreadable: {exc}"
            return record

        scoped = self.llm.scoped()
        try:
            reply = scoped.complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_coverage_prompt(
                            file_coverage, source, import_name, existing_tests
                        ),
                    },
                ]
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

        problem = validate_generated(code, import_name)
        if problem is not None:
            record.error = problem
            return record

        target = destination or self.destination_for(file_coverage)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f'"""Coverage tests for {import_name}.\n\n'
                f"Written by AUTEF ({GENERATED_MARKER}) to reach lines and\n"
                f'branches the existing suite never executed.\n"""\n\n'
                + code.strip()
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            record.error = f"could not write {target}: {exc}"
            return record

        record.test_file = str(target)
        record.accepted = True
        record.duration_s = time.time() - started
        logger.info("  wrote %s", target.name)
        return record

    def destination_for(self, file_coverage: FileCoverage) -> Path:
        stem = Path(file_coverage.path).stem
        root = (
            Path(self.layout.test_roots[0])
            if self.layout.test_roots
            else Path(self.layout.root) / "tests"
        )
        return reserve_path(root / f"test_{stem}_coverage.py")

    def _bill(self, record: GeneratedTest, scoped: LLMClient, started: float) -> None:
        record.prompt_tokens = scoped.usage.prompt_tokens
        record.completion_tokens = scoped.usage.completion_tokens
        record.cost_usd = scoped.usage.cost_usd
        record.duration_s = time.time() - started
