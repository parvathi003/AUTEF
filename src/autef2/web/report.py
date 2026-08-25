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
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _code_block(title: str, subtitle: str, body: str) -> str:
    if not body:
        return ""
    return (
        '<div class="file"><div class="fh"><b>' + _e(title) + "</b>"
        '<span>' + _e(subtitle) + "</span></div>"
        "<pre>" + _e(body) + "</pre></div>"
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
        parts.append(
            _code_block(entry.get("file") or "(not written)", subtitle,
                        entry.get("content", ""))
        )
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
 .file {{ margin:10px 0 18px; border:1px solid var(--line); border-radius:8px }}
 .fh {{ display:flex; justify-content:space-between; gap:12px; padding:9px 14px;
        background:var(--tint); border-radius:8px 8px 0 0; font-size:12.5px }}
 .fh span {{ color:var(--muted) }}
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
        f' · signed in as {_e(data.get("username"))}</p>')

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
        ("Working copy", layout.get("root")),
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
        for r in records:
            for a in r.get("attempts") or []:
                if not a.get("patch"):
                    continue
                verdict = "verified" if a.get("verified") else "rejected"
                if a.get("rejected"):
                    verdict += " — " + str(a["rejected"])[:160]
                add(_code_block(
                    f"{r.get('nodeid')}  ·  attempt {a.get('n')}: {a.get('strategy')}",
                    verdict, a.get("patch", "")))

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
            add(_metric("Killed", f"{mut.get('killed_after')} / {mut.get('total')}"))
            add(_metric("Newly killed", mut.get("newly_killed", 0)))
            add(_metric("Killer tests kept", mut.get("written", 0)))
            add("</div>")
            add('<p class="note">A killer test counts only if it passes on the '
                "original source and fails with the mutant applied. Passing both "
                "ways raises the score without testing anything.</p>")
            add(_written_section("The killer tests", mut.get("files") or [], ""))
        else:
            add('<div class="warn">' + _e(mut.get("skipped_reason")
                or "Not measured.") + "</div>")

    # -- spend ------------------------------------------------------------
    add("<h2>Model usage</h2><table>")
    for label, value in [
        ("Model calls", usage.get("calls", 0)),
        ("Prompt tokens", usage.get("prompt_tokens", 0)),
        ("Completion tokens", usage.get("completion_tokens", 0)),
        ("Cost (USD)", "%.6f" % usage.get("cost_usd", 0)),
        ("Wall clock", str(data.get("elapsed_s", 0)) + "s"),
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
        for path in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file():
                continue
            try:
                archive.write(path, str(Path("project") / path.relative_to(root)))
            except OSError:
                continue
    return buffer.getvalue()
