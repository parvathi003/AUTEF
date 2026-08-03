"""Repair ladders: one ordered list of strategies per root cause.

This is the concrete answer to "the repair step is generic". v1 had exactly one
prompt (``build_fix_prompt``) and sent every failure to it -- a broken import, a
wrong expected value and a mis-targeted patch all got the same instruction to
"fix the test function".

Here each root cause has its own ladder. Rung 1 is the narrowest repair that
could plausibly work and the cheapest context to send. Each later rung both
widens the edit (function -> whole file) and widens the evidence (add the
project tree, add sibling tests), because a rung is only tried after the
previous one was applied, re-run, and observed not to work.

Two invariants the orchestrator relies on:

* a strategy is never tried twice for the same test -- escalation means a
  *different* approach, not a retry of the same one;
* the ladder is ordered by cost, so the common case resolves at rung 1.
"""

from __future__ import annotations

from typing import Dict, List

from .models import RootCause, Strategy

_OUTPUT_RULES_FUNCTION = """
OUTPUT RULES (violating these makes the answer unusable):
- Return ONLY the corrected test function(s). No prose, no explanation.
- No markdown fences, no ``` of any kind.
- No import statements and no class header. Imports already exist in the file;
  if the fix genuinely needs a new import, use a local import inside the
  function body.
- Keep the function's original name exactly. Renaming it detaches it from the
  failing test id and the repair cannot be verified.
- Write the function at top level (no leading indentation); it will be
  re-indented into its class automatically.
- Preserve every assertion's strength. Do not delete assertions, do not replace
  a specific assertion with a weaker one, and never add a skip or xfail marker.
  A test that passes because it stopped checking anything is a failed repair.
""".strip()

_OUTPUT_RULES_FILE = """
OUTPUT RULES (violating these makes the answer unusable):
- Return the COMPLETE corrected test file, ready to run as-is.
- No prose, no explanation, no markdown fences.
- Keep every existing test function, with its original name. You may fix them,
  but you may not delete them or weaken their assertions.
- Never add skip or xfail markers.
""".strip()


def _strategy(
    id: str,
    label: str,
    rung: int,
    instruction: str,
    *,
    scope: str = "function",
    include_source: bool = True,
    include_full_test_file: bool = False,
    include_project_tree: bool = False,
    include_sibling_tests: bool = False,
) -> Strategy:
    rules = _OUTPUT_RULES_FILE if scope == "file" else _OUTPUT_RULES_FUNCTION
    return Strategy(
        id=id,
        label=label,
        rung=rung,
        system_prompt=f"{instruction.strip()}\n\n{rules}",
        scope=scope,
        include_source=include_source,
        include_full_test_file=include_full_test_file or scope == "file",
        include_project_tree=include_project_tree,
        include_sibling_tests=include_sibling_tests,
    )


LADDERS: Dict[RootCause, List[Strategy]] = {
    RootCause.IMPORT_ERROR: [
        _strategy(
            "fix_import_statement", "Correct the import path", 1,
            """
            You are repairing a Python test that fails because it imports
            something that is not importable. The project's real module layout
            is given below. Correct the import so it names the module and
            symbol as they actually exist. Do not create new modules and do not
            change any assertion.
            """,
            include_project_tree=True,
        ),
        _strategy(
            "bootstrap_import_path", "Make the package importable from the test", 2,
            """
            The import is still failing. The module exists but is not on the
            import path from where this test runs. Fix it inside the test
            function: import the module by the path it actually occupies, or
            insert the correct project directory onto sys.path before importing.
            Use the real directory layout given below, not a guess.
            """,
            include_project_tree=True,
            include_full_test_file=True,
        ),
        _strategy(
            "rewrite_imports_from_source", "Rewrite the test against the real API", 3,
            """
            Two import repairs have already failed. Read the source file and
            rewrite the whole test file so its imports and the names it uses
            match what that source actually defines. Keep every test function
            and every assertion.
            """,
            scope="file",
            include_project_tree=True,
        ),
    ],
    RootCause.COLLECTION_ERROR: [
        _strategy(
            "repair_syntax", "Repair the file so it can be collected", 1,
            """
            This test file cannot be imported at all -- pytest failed during
            collection, so none of its tests ran. Fix the error that prevents
            collection (syntax error, bad indentation, an unresolvable
            module-level statement). Change as little as possible and keep
            every test function intact.
            """,
            scope="file",
        ),
        _strategy(
            "rebuild_test_file", "Rebuild the file around the same tests", 2,
            """
            The file still cannot be collected. Rebuild it: correct module-level
            imports and setup, and keep every test function that was there,
            with the same names and the same assertions.
            """,
            scope="file",
            include_project_tree=True,
        ),
    ],
    RootCause.ASSERTION_MISMATCH: [
        _strategy(
            "align_expected_value", "Correct the expected value", 1,
            """
            A test asserts an expected value that does not match what the code
            produces. Read the source and decide what the code correctly
            returns for these inputs, then correct the expectation.

            Before changing anything, ask whether the expectation is right and
            the code is wrong. If the source has a genuine defect, do NOT
            paper over it: return the function unchanged and put the single
            line NO_TEST_FIX_NEEDED on its own line before it.
            """,
        ),
        _strategy(
            "correct_test_logic", "Correct how the test drives the code", 2,
            """
            Correcting the expected value did not fix it. The problem is in how
            the test sets up or exercises the code -- wrong construction
            arguments, missing state, calling the wrong method, or comparing
            the wrong thing. Fix the body so it exercises the behaviour it
            claims to test. Keep the assertion at least as strong as it was.
            """,
            include_full_test_file=True,
        ),
        _strategy(
            "rewrite_test_function", "Rewrite the test from the source contract", 3,
            """
            Two repairs have failed. Rewrite this test function from scratch so
            that it tests the same behaviour its name describes, against the
            real source below. Sibling tests in the same file show the
            conventions to follow. The rewritten test must make a specific,
            meaningful assertion about the behaviour -- not merely run without
            error.
            """,
            include_full_test_file=True,
            include_sibling_tests=True,
        ),
    ],
    RootCause.MOCK_MISCONFIGURATION: [
        _strategy(
            "fix_patch_target", "Point the patch at the right target", 1,
            """
            A mock or patch is misconfigured. The most common cause by far is
            patching where a symbol is *defined* instead of where it is
            *looked up*: if the module under test does `from x import y`, the
            patch target is `module_under_test.y`, not `x.y`. Read the source's
            imports below and correct the patch target.
            """,
        ),
        _strategy(
            "fix_mock_behaviour", "Correct the mock's spec and return values", 2,
            """
            The patch target is not the problem. The mock's configured
            behaviour is wrong: return_value, side_effect, the attributes it
            exposes, or the call it is asserted against does not match how the
            code under test uses it. Read the source, see exactly how the
            dependency is called, and configure the mock to match.
            """,
            include_full_test_file=True,
        ),
        _strategy(
            "rewrite_test_with_mocks", "Rewrite the test's isolation strategy", 3,
            """
            Two mock repairs have failed. Rewrite this test function with a
            mocking approach that matches how the source actually collaborates
            with its dependencies. Keep the behavioural assertion -- including
            any assertion about how the dependency was called.
            """,
            include_full_test_file=True,
            include_sibling_tests=True,
        ),
    ],
    RootCause.FIXTURE_SETUP_ERROR: [
        _strategy(
            "fix_fixture_usage", "Correct the fixture or setup", 1,
            """
            The test failed in setup, not in the test body -- the fixture,
            setUp or setup_method raised before the test could run. Fix the
            setup code. It lives outside the failing function, so return the
            complete corrected file.
            """,
            scope="file",
        ),
        _strategy(
            "inline_setup", "Give the test its own setup", 2,
            """
            The shared setup is still failing. Give this test the state it
            needs directly, inside the test function, rather than depending on
            the broken shared fixture. Leave the other tests and the existing
            fixture alone.
            """,
            include_full_test_file=True,
        ),
        _strategy(
            "rebuild_setup", "Rebuild setup around the real source API", 3,
            """
            Two setup repairs have failed. Rebuild the file's fixtures and
            setup so they construct the objects the source below actually
            defines, with the arguments it actually takes. Keep every test
            function and assertion.
            """,
            scope="file",
            include_project_tree=True,
        ),
    ],
    RootCause.API_MISUSE: [
        _strategy(
            "correct_call_signature", "Match the real signature", 1,
            """
            The test calls the code under test incorrectly -- wrong argument
            count, wrong keyword names, wrong types, or an attribute or method
            that does not exist. Read the source's actual definitions below and
            correct the call. Do not change what the test asserts.
            """,
        ),
        _strategy(
            "align_with_public_api", "Use the API the source really exposes", 2,
            """
            The call is still wrong. The test is using an API shape the source
            does not have. Work out from the source what the correct sequence
            of construction and calls is, and rewrite the body to use it, while
            still asserting the same behaviour.
            """,
            include_full_test_file=True,
        ),
        _strategy(
            "rewrite_test_function", "Rewrite against the source contract", 3,
            """
            Two repairs have failed. Rewrite this test function against the
            real source contract below, testing the behaviour its name
            describes, with a specific and meaningful assertion.
            """,
            include_full_test_file=True,
            include_sibling_tests=True,
        ),
    ],
    RootCause.FLAKY_NONDETERMINISM: [
        _strategy(
            "pin_nondeterminism", "Pin the nondeterministic input", 1,
            """
            This test depends on something that varies between runs -- current
            time, random values, iteration order of an unordered collection, or
            filesystem ordering. Make it deterministic by pinning or freezing
            that input, or by asserting in an order-independent way. Do not
            weaken what is being checked.
            """,
        ),
        _strategy(
            "isolate_shared_state", "Remove the shared-state dependency", 2,
            """
            The test is still unstable. It likely depends on state left behind
            by another test or on a module-level singleton. Give it isolated
            state so its result does not depend on execution order.
            """,
            include_full_test_file=True,
        ),
    ],
    RootCause.UNKNOWN: [
        _strategy(
            "targeted_repair", "Repair from the traceback", 1,
            """
            Repair this failing test. Work from the traceback: identify the
            exact statement that failed and why, then make the smallest change
            that makes the test correct against the source below. Do not change
            what the test is checking.
            """,
        ),
        _strategy(
            "rewrite_test_function", "Rewrite the test function", 2,
            """
            The targeted repair did not work. Rewrite the whole test function
            against the source below, keeping the behaviour it tests and
            asserting it specifically.
            """,
            include_full_test_file=True,
            include_sibling_tests=True,
        ),
        _strategy(
            "rewrite_test_file", "Rebuild the test file", 3,
            """
            Function-level repair has failed twice, so the problem is probably
            outside the function -- imports, module-level setup, or a fixture.
            Return the complete corrected file, keeping every test function and
            every assertion.
            """,
            scope="file",
            include_project_tree=True,
        ),
    ],
}

#: The model can signal that the test is right and the source is wrong. We
#: honour it rather than forcing a fix, because forcing one manufactures a
#: false pass over a real production bug.
NO_FIX_SENTINEL = "NO_TEST_FIX_NEEDED"


def ladder_for(cause: RootCause) -> List[Strategy]:
    """The ordered strategies for a cause, falling back to the generic ladder."""
    return LADDERS.get(cause) or LADDERS[RootCause.UNKNOWN]


def strategy_by_id(strategy_id: str) -> Strategy | None:
    for ladder in LADDERS.values():
        for strategy in ladder:
            if strategy.id == strategy_id:
                return strategy
    return None


def all_strategy_ids() -> List[str]:
    seen: List[str] = []
    for ladder in LADDERS.values():
        for strategy in ladder:
            if strategy.id not in seen:
                seen.append(strategy.id)
    return seen
