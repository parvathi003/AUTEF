"""AUTEF v2 - Agentic UnitTest Enhancement Framework, generalised.

Two changes over v1 (``src/agenticapp``):

1. Project portability. Nothing is hardcoded. The project name, source roots
   and test roots are derived from the uploaded archive, tests run under
   pytest, file locations come from the traceback rather than from guessing
   filenames out of test class names, and each project's dependencies are
   installed into its own virtual environment.

2. Diagnosed repair. The single generic fix prompt is replaced by three
   agents - failure analysis, repair strategy, autofix - that identify the
   root cause, pick a matching repair, verify the result, and escalate to a
   different strategy when the repair does not hold.

v1 is left untouched; the evaluation harness uses it as the baseline arm.
"""

__version__ = "2.0.0-mvp"
