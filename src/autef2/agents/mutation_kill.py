"""Mutation Agent: write a test that catches a change the suite missed.

v1's ``generate_mutation_prompt`` did this and its shape is kept: show the
source, the tests that exist, and the mutation that survived, then ask for a
test that would have caught it.

What is added is the check. A test written to kill a mutant has one defining
property -- it fails when the mutant is applied and passes when it is not -- and
that is verifiable, cheaply, by running it twice. v1 wrote the test and reported
an improved mutation score without ever confirming the new test killed anything.
A test that passes in both states is worse than useless here: it inflates the
score while testing nothing about the mutated behaviour.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from ..config import AutefConfig
from ..llm import LLMClient, LLMError
from ..models import GeneratedTest, Mutant, ProjectLayout
from ..patcher import strip_code_fences
from .generation import GENERATED_MARKER, reserve_path, validate_generated

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a Python test engineer specialising in mutation testing. Given a "
    "change to the source that the existing tests failed to detect, you write a "
    "test that would detect it. You return only Python code."
)


def build_mutation_prompt(
    mutant: Mutant,
    source: str,
    import_name: str,
    existing_tests: str,
) -> str:
    """v1's mutation prompt, with the surviving change stated exactly."""
    existing = existing_tests.strip()
    existing_section = (
        f"""
Tests that already exist and did NOT catch this change:
```python
{existing[:4000]}
```
"""
        if existing
        else "\nThis module has no tests yet.\n"
    )

    return f"""The test suite did not notice this change to the source.

Import the module as: `{import_name}`
File: {Path(mutant.file).name}, line {mutant.lineno}
Change applied: {mutant.operator}

    before:  {mutant.original}
    after:   {mutant.mutated}

Full source of the module:
```python
{source[:10000]}
```
{existing_section}
Write a test that fails when the changed version is in place and passes with the
original.

Requirements:
- Import from `{import_name}`. That import works as written; add no sys.path code.
- Choose inputs for which the two versions genuinely differ. Work out from the
  source what value the original produces, and assert exactly that value. If you
  assert what the *mutant* produces, the test will pass on the mutant and fail
  on correct code, which is the opposite of what is wanted.
- One focused test is better than several vague ones.
- No network, no database, no sleeping. Use unittest.mock for collaborators.
- Return only Python code, no markdown fences and no commentary.
"""


class MutationKillAgent:
    """Writes a test intended to kill one surviving mutant."""

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
        mutant: Mutant,
        *,
        import_name: str,
        existing_tests: str = "",
        destination: Optional[Path] = None,
    ) -> GeneratedTest:
        started = time.time()
        record = GeneratedTest(
            module=mutant.file,
            module_import=import_name,
            units=[f"{Path(mutant.file).name}:{mutant.lineno} {mutant.operator}"],
        )

        if self.llm is None:
            record.error = "no model configured"
            return record

        try:
            source = Path(mutant.file).read_text(encoding="utf-8", errors="replace")
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
                        "content": build_mutation_prompt(
                            mutant, source, import_name, existing_tests
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

        target = destination or self.destination_for(mutant)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f'"""Mutation tests for {import_name}.\n\n'
                f"Written by AUTEF ({GENERATED_MARKER}) to catch changes the\n"
                f'existing suite did not notice.\n"""\n\n'
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
        return record

    def destination_for(self, mutant: Mutant) -> Path:
        stem = Path(mutant.file).stem
        root = (
            Path(self.layout.test_roots[0])
            if self.layout.test_roots
            else Path(self.layout.root) / "tests"
        )
        return reserve_path(root / f"test_{stem}_mutation.py")

    def _bill(self, record: GeneratedTest, scoped: LLMClient, started: float) -> None:
        record.prompt_tokens = scoped.usage.prompt_tokens
        record.completion_tokens = scoped.usage.completion_tokens
        record.cost_usd = scoped.usage.cost_usd
        record.duration_s = time.time() - started
