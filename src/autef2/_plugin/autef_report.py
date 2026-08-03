"""Pytest plugin injected into the project under test.

Standard library only, on purpose. It is loaded into whatever interpreter the
project's tests run under, so it must not need anything installed beyond pytest
itself -- no pytest-json-report, no reportlog. It writes one JSON object per
test report to ``$AUTEF_REPORT_PATH`` as JSONL.

What matters here is that we capture the *traceback frames*, not just the
message. Those frames are how the resolver finds the test file and the source
files involved, which replaces v1's habit of guessing filenames from test class
names.
"""

import json
import os

REPORT_PATH = os.environ.get("AUTEF_REPORT_PATH")


def _emit(record):
    if not REPORT_PATH:
        return
    try:
        with open(REPORT_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Never let reporting break the test run.
        pass


def _traceback_entries(longrepr):
    """Pull (path, lineno, message) out of pytest's traceback representation."""
    entries = []
    reprtraceback = getattr(longrepr, "reprtraceback", None)

    if reprtraceback is None:
        # Chained exceptions ("during handling of the above..."): the last link
        # is the one that actually failed the test.
        chain = getattr(longrepr, "chain", None)
        if chain:
            try:
                reprtraceback = chain[-1][0]
            except (IndexError, TypeError):
                reprtraceback = None

    if reprtraceback is None:
        return entries

    for entry in getattr(reprtraceback, "reprentries", None) or []:
        fileloc = getattr(entry, "reprfileloc", None)
        if fileloc is None:
            continue
        entries.append(
            {
                "path": str(getattr(fileloc, "path", "")),
                "lineno": int(getattr(fileloc, "lineno", 0) or 0),
                "message": str(getattr(fileloc, "message", "")),
            }
        )
    return entries


def _crash(longrepr):
    reprcrash = getattr(longrepr, "reprcrash", None)
    if reprcrash is None:
        return {"path": "", "lineno": 0, "message": ""}
    return {
        "path": str(getattr(reprcrash, "path", "")),
        "lineno": int(getattr(reprcrash, "lineno", 0) or 0),
        "message": str(getattr(reprcrash, "message", "")),
    }


def pytest_runtest_logreport(report):
    # A passing test reports three phases; only "call" is the result. A failure
    # in setup or teardown is a real failure and must not be dropped.
    if report.when != "call" and not report.failed:
        return

    record = {
        "kind": "test",
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "duration": float(getattr(report, "duration", 0.0) or 0.0),
        "longrepr": "",
        "crash": {"path": "", "lineno": 0, "message": ""},
        "frames": [],
    }

    if report.longrepr is not None:
        record["longrepr"] = str(report.longrepr)
        record["crash"] = _crash(report.longrepr)
        record["frames"] = _traceback_entries(report.longrepr)

    _emit(record)


def pytest_collectreport(report):
    """A test file that cannot even be imported never reaches logreport."""
    if report.outcome != "failed":
        return
    record = {
        "kind": "collect",
        "nodeid": report.nodeid,
        "when": "collect",
        "outcome": "failed",
        "duration": 0.0,
        "longrepr": str(report.longrepr) if report.longrepr is not None else "",
        "crash": _crash(report.longrepr) if report.longrepr is not None else {},
        "frames": _traceback_entries(report.longrepr) if report.longrepr else [],
    }
    _emit(record)


def pytest_sessionfinish(session, exitstatus):
    _emit(
        {
            "kind": "session",
            "exitstatus": int(exitstatus),
            "collected": int(getattr(session, "testscollected", 0) or 0),
            "failed": int(getattr(session, "testsfailed", 0) or 0),
        }
    )
