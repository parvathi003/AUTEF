"""The downloadable run report.

Three formats, all built from the same session state:

* **HTML** -- one self-contained file, opens in any browser with no server and
  no network. It carries the full text of every test the run wrote and every
  repair it applied, because a report that says "4 tests generated" without
  showing them cannot be checked by the person reading it.
* **JSON** -- the same content, for the record and for further analysis.
* **ZIP**  -- the working copy as the run left it: the project plus the tests
  that were generated and the repairs that were applied.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Never packed into the download: caches, virtualenvs and version control.
SKIP_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules", ".autef",
}
MAX_EMBEDDED_CHARS = 200_000


def build(session) -> Dict[str, Any]:
    """Everything known about the run, including the code it wrote."""
    from .server import _snapshot

    state = _snapshot(session)
    layout = session.state.get("layout")
    root = Path(layout.root) if layout is not None else None

    def read(path: str) -> str:
        if not path or root is None:
            return ""
        try:
            target = Path(path)
            if not target.is_absolute():
                target = root / target
            target = target.resolve()
            target.relative_to(root.resolve())
            return target.read_text(encoding="utf-8", errors="replace")[
                :MAX_EMBEDDED_CHARS
            ]
        except (OSError, ValueError):
            return ""

    # Embed the full text of everything the run wrote.
    for section in ("generation", "coverage", "mutation"):
        block = state.get(section)
        if not block:
            continue
        entries = block.get("records") if section == "generation" else block.get("files")
        for entry in entries or []:
            entry["content"] = read(entry.get("path", ""))

    state["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["project"] = (state.get("layout") or {}).get("name") or "project"
    return state


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _metric(label: str, value: Any, note: str = "") -> str:
    return (
        '<div class="m"><div class="ml">' + _e(label) + "</div>"
        '<div class="mv">' + _e(value) + "</div>"
        + ('<div class="mn">' + _e(note) + "</div>" if note else "")
        + "</div>"
    )


def _code_block(title: str, subtitle: str, body: str, *, open_: bool = False) -> str:
    """One collapsible disclosure. ``details`` so it needs no JavaScript."""
    if not body:
        return ""
    return (
        '<details class="block"' + (" open" if open_ else "") + ">"
        "<summary><b>" + _e(title) + "</b>"
        + ("<span>" + _e(subtitle) + "</span>" if subtitle else "")
        + "</summary><pre>" + _e(body) + "</pre></details>"
    )


def _listing(code: str, start_line: int, error_line: Optional[int]) -> str:
    """A numbered code listing with the blamed line marked.

    The traceback names a line; showing it in place is the difference between
    "this test failed" and "this is what failed, and here."
    """
    rows = []
    for offset, line in enumerate(str(code or "").split("\n")):
        number = (start_line or 1) + offset
        bad = error_line is not None and number == error_line
        rows.append(
            '<div class="cl' + (" bad" if bad else "") + '">'
            '<span class="ln">' + str(number) + "</span>"
            '<span class="lt">' + (_e(line) or "&nbsp;") + "</span></div>"
        )
    return '<div class="listing">' + "".join(rows) + "</div>"


def _failing_block(record: Dict[str, Any], index: int) -> str:
    """The test as it stood when it failed, with the error line marked."""
    failing = record.get("failing")
    if not failing or not failing.get("code"):
        return ""
    subtitle = str(failing.get("exception") or "")
    if failing.get("message"):
        subtitle += ": " + str(failing["message"])
    return (
        '<details class="block failing" open><summary><b>the failing test</b>'
        "<span>" + _e(subtitle) + "</span></summary>"
        + _listing(
            failing.get("code", ""),
            failing.get("start_line") or 1,
            failing.get("error_line"),
        )
        + "</details>"
    )


def _band(title: str, subtitle: str) -> str:
    """The header a group of disclosures hangs from."""
    return (
        '<div class="fileband"><b>' + _e(title) + "</b>"
        "<span>" + _e(subtitle) + "</span></div>"
    )


def _split_tests(source: str):
    """Top-level definitions in a Python file, plus whatever precedes them.

    A generated suite is one file, but the unit worth reading is one test, so
    they are listed separately rather than as a single wall of code.
    """
    header: List[str] = []
    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for line in str(source or "").split("\n"):
        match = re.match(r"^(def|class)\s+([A-Za-z_]\w*)", line)
        if match:
            if current:
                blocks.append(current)
            current = {"kind": match.group(1), "name": match.group(2), "lines": [line]}
        elif current:
            current["lines"].append(line)
        else:
            header.append(line)
    if current:
        blocks.append(current)

    for block in blocks:
        block["code"] = "\n".join(block["lines"]).rstrip()
        block["tests"] = (
            len(re.findall(r"def\s+test\w*", block["code"]))
            if block["kind"] == "class"
            else 1
        )
    return "\n".join(header).strip(), [b for b in blocks if b["code"].strip()]


def _mutant_table(mutants: List[Dict[str, Any]]) -> str:
    """Every mutant and its fate, survivors first.

    A mutation score is a claim about a suite's sensitivity, and "one survived"
    is not a finding until you can say which one and what happened to it. This
    is the table an examiner needs in order to check the number.
    """
    if not mutants:
        return ""

    survivors = [m for m in mutants if not m.get("killed") and not m.get("error")]
    unscored = [m for m in mutants if m.get("error")]
    killed = [m for m in mutants if m.get("killed")]

    rows = []
    for m in survivors + unscored + killed:
        if m.get("error"):
            verdict = '<span class="tag neutral">not scored</span>'
            note = _e(m["error"])
        elif m.get("killed"):
            verdict = '<span class="tag ok">caught</span>'
            note = ("caught by a test written for it"
                    if m.get("killed_after_generation")
                    else "caught by " + _e(str(m.get("killed_by") or "").split("::")[-1]))
        else:
            verdict = '<span class="tag bad">survived</span>'
            if m.get("attempt_error"):
                note = "killer test rejected — " + _e(m["attempt_error"])
            elif m.get("attempted"):
                note = "a killer test was attempted and did not hold"
            else:
                note = "no test detects this change; none was attempted"
        rows.append(
            "<tr><td class='mono'>" + _e(m.get("file")) + ":" + _e(m.get("line"))
            + "</td><td>" + _e(m.get("operator")) + "</td>"
            "<td class='mono diff'><div class='minus'>- " + _e(m.get("original"))
            + "</div><div class='plus'>+ " + _e(m.get("mutated")) + "</div></td>"
            "<td>" + verdict + "</td><td>" + note + "</td></tr>"
        )

    summary = (
        f"<b>{len(survivors)} mutant(s) survived</b> — the suite did not notice "
        "these changes. Each is a gap in the tests, unless the change cannot "
        "alter behaviour at all (an equivalent mutant), which no test can catch."
        if survivors else "Every mutant was caught."
    )
    if unscored:
        summary += (
            f" {len(unscored)} could not be scored and are excluded from the "
            "score rather than counted as caught."
        )

    return (
        "<h3>Every mutant, and what happened to it</h3>"
        "<table><tr><th>Where</th><th>Operator</th><th>The change</th>"
        "<th>Result</th><th>Detail</th></tr>" + "".join(rows) + "</table>"
        '<p class="note">' + summary + "</p>"
    )


def _written_section(heading: str, entries: List[Dict[str, Any]], note: str) -> str:
    if not entries:
        return ""
    parts = ["<h3>" + _e(heading) + "</h3>", '<p class="note">' + _e(note) + "</p>"]
    for entry in entries:
        status = "kept" if entry.get("kept") else "rejected"
        subtitle = (
            f"{status} · {entry.get('collected', 0)} collected, "
            f"{entry.get('passing', 0)} passing"
        )
        if entry.get("error"):
            subtitle += " · " + str(entry["error"])[:160]
        parts.append(_band(entry.get("file") or "(not written)", subtitle))

        head, tests = _split_tests(entry.get("content", ""))
        for block in tests:
            meta = (
                f"{block['tests']} test(s)" if block["kind"] == "class" else "test case"
            )
            parts.append(
                _code_block(f"{block['kind']} {block['name']}", meta, block["code"])
            )
        if head:
            parts.append(_code_block("imports and setup", "", head))
        if not tests:
            parts.append('<p class="note">Nothing was written.</p>')
    return "".join(parts)


def render_html(data: Dict[str, Any]) -> str:
    layout = data.get("layout") or {}
    env = data.get("environment") or {}
    before = data.get("before") or {}
    after = data.get("after") or {}
    usage = data.get("usage") or {}
    records = data.get("records") or []
    gen = data.get("generation") or {}
    cov = data.get("coverage") or {}
    mut = data.get("mutation") or {}

    fixed = [r for r in records if r.get("fixed")]
    weakened = [r for r in records if r.get("weakened")]
    regressed = [r for r in records if r.get("regression")]

    out: List[str] = []
    add = out.append

    add(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>AUTEF v2 report — {_e(data.get('project'))}</title>
<style>
 :root {{ --ink:#191c20; --muted:#5c6068; --line:#d8dee9; --bg:#ffffff;
          --tint:#f4f6fb; --navy:#415f91; --green:#2e7d32; --red:#ba1a1a; }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; padding:40px 32px 80px; background:var(--bg); color:var(--ink);
   font:14px/1.6 "Segoe UI",Roboto,system-ui,Arial,sans-serif; }}
 .wrap {{ max-width:1000px; margin:0 auto }}
 h1 {{ font-size:26px; margin:0 0 4px }}
 h2 {{ font-size:18px; margin:38px 0 10px; padding-bottom:6px;
       border-bottom:1px solid var(--line) }}
 h3 {{ font-size:14px; margin:24px 0 6px }}
 .sub {{ color:var(--muted); margin:0 0 6px }}
 .note {{ color:var(--muted); font-size:12.5px; margin:4px 0 10px }}
 .ms {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0 }}
 .m {{ flex:1 1 140px; padding:12px 14px; background:var(--tint); border-radius:10px }}
 .ml {{ font-size:11.5px; color:var(--muted) }}
 .mv {{ font-size:22px; font-weight:600; margin-top:2px }}
 .mn {{ font-size:11.5px; color:var(--muted) }}
 table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:12.5px }}
 th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
          vertical-align:top }}
 th {{ font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:var(--muted) }}
 code,.mono {{ font-family:Consolas,"Courier New",monospace; font-size:12px }}
 pre {{ margin:0; padding:14px 16px; background:#1b1f27; color:#d7dce5;
        border-radius:0 0 8px 8px; overflow:auto; white-space:pre;
        font-family:Consolas,"Courier New",monospace; font-size:12px; line-height:1.6 }}
 .fileband {{ display:flex; justify-content:space-between; gap:12px;
        padding:9px 14px; margin-top:14px; background:var(--tint);
        border:1px solid var(--line); border-bottom:none;
        border-radius:8px 8px 0 0; font-size:12.5px }}
 .fileband span {{ color:var(--muted) }}
 details.block {{ border:1px solid var(--line); border-top:none }}
 details.block:last-of-type {{ border-radius:0 0 8px 8px }}
 details.block > summary {{ display:flex; justify-content:space-between;
        gap:12px; padding:8px 14px; cursor:pointer; font-size:12.5px;
        font-family:Consolas,"Courier New",monospace }}
 details.block > summary:hover {{ background:var(--tint) }}
 details.block > summary span {{ color:var(--muted); font-size:11.5px;
        font-family:"Segoe UI",Roboto,Arial,sans-serif }}
 details.block > pre {{ border-radius:0 }}
 td.diff {{ line-height:1.5 }}
 td.diff .minus {{ color:#b3261e }}
 td.diff .plus {{ color:#2e7d32 }}
 details.block.failing > summary {{ border-left:3px solid var(--red) }}
 .listing {{ padding:10px 0; background:#1b1f27; color:#d7dce5;
        font-family:Consolas,"Courier New",monospace; font-size:12px;
        line-height:1.62; overflow-x:auto }}
 .cl {{ display:flex; gap:12px; padding:0 14px }}
 .cl .ln {{ flex:0 0 auto; min-width:30px; text-align:right; color:#6b7684 }}
 .cl .lt {{ white-space:pre }}
 .cl.bad {{ background:rgba(255,90,80,.17); box-shadow:inset 3px 0 0 #ff6b5e }}
 .cl.bad .ln {{ color:#ff9c92; font-weight:700 }}
 .tag {{ display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px }}
 .ok {{ background:#dff3e0; color:#17501a }} .bad {{ background:#ffdad6; color:#5b0e0e }}
 .neutral {{ background:#e8eaf1; color:#44474e }}
 .warn {{ padding:12px 16px; background:#fff4e5; border-radius:8px; margin:12px 0 }}
 footer {{ margin-top:50px; padding-top:14px; border-top:1px solid var(--line);
           color:var(--muted); font-size:12px }}
 @media print {{ body {{ padding:0 }} pre {{ white-space:pre-wrap }} }}
</style></head><body><div class="wrap">""")

    add(f"<h1>AUTEF v2 — {_e(data.get('project'))}</h1>")
    add(f'<p class="sub">Nine-stage run · generated {_e(data.get("generated_at"))}'
        '</p>')

    # -- headline ---------------------------------------------------------
    add("<h2>Result</h2>")
    add('<div class="ms">')
    add(_metric("Passing", after.get("passed", before.get("passed", 0)),
                f"was {before.get('passed', 0)}"))
    add(_metric("Failing", after.get("failed", before.get("failed", 0)),
                f"was {before.get('failed', 0)}"))
    add(_metric("Repaired", f"{len(fixed)} / {len(records)}"))
    add(_metric("Weakened", len(weakened),
                "fixes that gutted an assertion" if weakened else "none"))
    add(_metric("Regressions", len(regressed),
                "broke a passing test" if regressed else "none"))
    add(_metric("Cost", "$%.4f" % usage.get("cost_usd", 0),
                f"{usage.get('calls', 0)} model calls"))
    add("</div>")
    add('<p class="note">Weakening rate is reported beside fix rate because fix '
        "rate, attempts and cost can all be improved by deleting an assertion. "
        "A test that asserts nothing always passes.</p>")

    # -- stages 1 and 2 ---------------------------------------------------
    add("<h2>Stages 1 &amp; 2 — the project and its environment</h2>")
    add("<table>")
    for label, value in [
        ("Project", layout.get("name")),
        ("Layout style", layout.get("style")),
        ("Test files", layout.get("test_files")),
        ("Declared dependencies", layout.get("dependencies")),
        ("Installable", "yes" if layout.get("installable") else "no"),
        ("Isolated virtualenv", "yes" if env.get("isolated") else "no"),
        ("Packages installed", len(env.get("installed") or [])),
        # Not the absolute path: it is a temp directory on the operator's
        # machine, carries their account name, and means nothing to a reader.
        ("Working copy", Path(str(layout.get("root") or "")).name),
    ]:
        add(f"<tr><th>{_e(label)}</th><td class='mono'>{_e(value)}</td></tr>")
    add("</table>")
    if layout.get("no_tests"):
        add('<div class="warn">This project ships <b>no test files</b>. Nothing '
            "failing is therefore not the same as everything passing.</div>")
    for warning in env.get("warnings") or []:
        add('<div class="warn">' + _e(warning) + "</div>")

    # -- stage 3 ----------------------------------------------------------
    add("<h2>Stage 3 — the suite as it arrived</h2>")
    add('<div class="ms">')
    add(_metric("Passing", before.get("passed", 0)))
    add(_metric("Failing", before.get("failed", 0)))
    add(_metric("Collection errors", before.get("collection_errors", 0)))
    add(_metric("Suite time", str(before.get("duration_s", 0)) + "s"))
    add("</div>")
    add('<p class="note">This snapshot is never rewritten. Later stages add test '
        "files, and the result above compares against this.</p>")

    # -- stage 4 ----------------------------------------------------------
    if gen:
        add("<h2>Stage 4 — generated tests</h2>")
        add('<div class="ms">')
        add(_metric("Modules considered", gen.get("considered", 0)))
        add(_metric("Files kept", gen.get("accepted", 0)))
        add(_metric("Tests added", gen.get("tests_added", 0)))
        add("</div>")
        add(_written_section(
            "The code that was written", gen.get("records") or [],
            "A file is kept only if pytest can run it. Generated tests that fail "
            "are kept on purpose: they are the repair loop's input."))

    # -- stages 5 and 6 ---------------------------------------------------
    if records:
        add("<h2>Stages 5 &amp; 6 — diagnosis and repair</h2>")
        add("<table><tr><th>Test</th><th>Root cause</th><th>Result</th>"
            "<th>Attempts</th><th>Strategies</th></tr>")
        for r in records:
            status = ('<span class="tag ok">fixed</span>' if r.get("fixed")
                      else '<span class="tag neutral">skipped</span>'
                      if r.get("skipped_reason")
                      else '<span class="tag bad">not fixed</span>')
            strategies = " &rarr; ".join(
                a.get("strategy", "") for a in r.get("attempts") or []
            )
            add(f"<tr><td class='mono'>{_e(r.get('nodeid'))}</td>"
                f"<td>{_e(r.get('cause') or '-')}</td><td>{status}</td>"
                f"<td>{len(r.get('attempts') or [])}</td>"
                f"<td class='mono'>{strategies or '-'}</td></tr>")
        add("</table>")

        add("<h3>Every repair attempt, in full</h3>")
        add('<p class="note">Escalation is the contribution: when a fix does not '
            "verify, the next attempt uses a different strategy rather than "
            "repeating the same one.</p>")
        for index, r in enumerate(records):
            attempts = [a for a in r.get("attempts") or [] if a.get("patch")]
            if not attempts and not r.get("failing"):
                continue
            add(_band(
                r.get("nodeid"),
                f"{len(attempts)} attempt(s) · {r.get('cause') or 'unknown cause'}",
            ))
            add(_failing_block(r, index))
            for a in attempts:
                verdict = "verified" if a.get("verified") else "rejected"
                if a.get("rejected"):
                    verdict += " — " + str(a["rejected"])[:160]
                # The one that worked opens by default; the rest are there to
                # show what was tried first.
                add(_code_block(
                    f"attempt {a.get('n')}: {a.get('strategy')}",
                    verdict, a.get("patch", ""), open_=bool(a.get("verified"))))

    # -- stage 7 ----------------------------------------------------------
    if cov:
        add("<h2>Stage 7 — coverage</h2>")
        if cov.get("measured"):
            add('<div class="ms">')
            add(_metric("Line coverage", f"{cov.get('line_after')}%",
                        f"was {cov.get('line_before')}%"))
            add(_metric("Branch coverage", f"{cov.get('branch_after')}%",
                        f"was {cov.get('branch_before')}%"))
            add(_metric("Statements", cov.get("statements", 0)))
            add(_metric("Tests written", cov.get("written", 0)))
            add("</div>")
            add(_written_section("The coverage tests", cov.get("files") or [],
                                 "Written for lines and branches nothing reached."))
        else:
            add('<div class="warn">Coverage could not be measured'
                + (": " + _e(cov.get("error")) if cov.get("error") else "") + "</div>")

    # -- stage 8 ----------------------------------------------------------
    if mut:
        add("<h2>Stage 8 — mutation</h2>")
        if mut.get("measured"):
            add('<div class="ms">')
            add(_metric("Mutation score", f"{mut.get('score_after')}%",
                        f"was {mut.get('score_before')}%"))
            add(_metric("Killed",
                        f"{mut.get('killed_after')} / {mut.get('scored') or mut.get('total')}",
                        "of the mutants a verdict was reached on"))
            add(_metric("Newly killed", mut.get("newly_killed", 0)))
            add(_metric("Killer tests kept", mut.get("written", 0)))
            add("</div>")
            add('<p class="note">A killer test counts only if it passes on the '
                "original source and fails with the mutant applied. Passing both "
                "ways raises the score without testing anything.</p>")
            if mut.get("unscored"):
                add('<div class="warn">' + _e(str(mut.get("unscored")))
                    + " mutant(s) reached no verdict"
                    + (" because the phase ran out of its time budget"
                       if mut.get("budget_exhausted") else "")
                    + ". They are left out of the score rather than counted as "
                    "survivors: not catching a mutant and never finding out are "
                    "different facts.</div>")
            if mut.get("excluded"):
                add('<p class="note">Scored against the tests that pass on '
                    "unmutated source. " + _e(str(len(mut["excluded"])))
                    + " already-failing test(s) were excluded, because a test "
                    "that was broken before the mutant cannot show that the "
                    "suite caught it: <code>"
                    + _e(", ".join(mut["excluded"][:6]))
                    + ("..." if len(mut["excluded"]) > 6 else "")
                    + "</code></p>")
            add(_mutant_table(mut.get("mutants") or []))
            add(_written_section("The killer tests", mut.get("files") or [], ""))
        else:
            add('<div class="warn">' + _e(mut.get("skipped_reason")
                or "Not measured.") + "</div>")

    # -- what was removed, and what did not run ---------------------------
    quarantined = data.get("quarantined") or []
    if quarantined:
        add("<h2>Tests AUTEF removed</h2>")
        add('<p class="note">These were written by AUTEF and could not be '
            "repaired, so they were taken back out: a suite handed back redder "
            "than the one uploaded is not an improvement. Each one is kept in "
            "full beside the test file it came from, in a "
            "<code>quarantined_*.py</code> that pytest does not collect. "
            "Nothing the project itself wrote is ever removed.</p>")
        add("<table><tr><th>Test</th><th>Diagnosed as</th><th>Why it went</th></tr>")
        for item in quarantined:
            add("<tr><td><code>" + _e(item.get("nodeid", "")) + "</code></td>"
                + "<td>" + _e(item.get("root_cause") or "-") + "</td>"
                + "<td>" + _e(item.get("reason", "")) + "</td></tr>")
        add("</table>")
        if any(q.get("root_cause") == "production_bug" for q in quarantined):
            add('<p class="note"><strong>Worth reading rather than '
                "dismissing:</strong> a removal diagnosed as "
                "<code>production_bug</code> is AUTEF saying the test was right "
                "and the code is wrong. Repairing such a test would have hidden "
                "a real defect, so it was refused.</p>")

    skips = data.get("stage_skips") or {}
    if skips:
        add("<h2>What did not run</h2>")
        add('<p class="note">A stage that declines to run is not a stage that '
            "ran and found nothing. Both used to look the same here.</p>")
        add("<table><tr><th>Stage</th><th>Reason</th></tr>")
        for key, reason in skips.items():
            add(f"<tr><th>{_e(key)}</th><td>{_e(reason)}</td></tr>")
        add("</table>")

    # -- spend ------------------------------------------------------------
    add("<h2>Model usage</h2><table>")
    for label, value in [
        ("Model calls", usage.get("calls", 0)),
        ("Prompt tokens", usage.get("prompt_tokens", 0)),
        ("Completion tokens", usage.get("completion_tokens", 0)),
        ("Cost (USD)", "%.6f" % usage.get("cost_usd", 0)),
        ("Wall clock", str(data.get("elapsed_s", 0)) + "s"),
        # Named so a figure quoted from this report can be attributed to a
        # configuration. A reasoning model refuses temperature 0, so runs are
        # not bit-identical and the settings are the only thing that pins them.
        ("Model", (data.get("settings") or {}).get("model", "")),
        ("Thinking effort",
         (data.get("settings") or {}).get("reasoning_effort") or "off"),
        ("Escalation rungs", (data.get("settings") or {}).get("max_attempts", "")),
    ]:
        add(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>")
    add("</table>")

    add("<footer>Produced by AUTEF v2. Generated tests are a starting point, "
        "not a specification: they were written against observed behaviour and "
        "reviewed by no one.</footer>")
    add("</div></body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def build_zip(session) -> Optional[bytes]:
    """The working copy as the run left it, plus the HTML and JSON reports."""
    layout = session.state.get("layout")
    if layout is None:
        return None
    root = Path(layout.root)

    data = build(session)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("autef2-report.html", render_html(data))
        archive.writestr(
            "autef2-report.json", json.dumps(data, indent=2, default=str)
        )
        omitted = []
        for path in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            try:
                archive.write(path, str(Path("project") / path.relative_to(root)))
            except OSError as exc:
                # Silently dropping a file makes the archive a quiet lie: on
                # Windows a path over 260 characters fails here, and a reader
                # comparing the zip to the project would find files missing
                # with nothing to say why.
                omitted.append(f"{path.relative_to(root)}: {exc}")
        if omitted:
            archive.writestr(
                "OMITTED.txt",
                "These files could not be added to the archive:\n\n"
                + "\n".join(omitted)
                + "\n",
            )
            logger.warning("%d file(s) omitted from the archive", len(omitted))
    return buffer.getvalue()
