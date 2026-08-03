# AUTEF v2

A rebuild of the Agentic UnitTest Enhancement Framework addressing its two
limitations: it ran on one application, and its repair step was generic.

v1 (`src/agenticapp`) is untouched. The evaluation harness uses its repair
prompt as the baseline arm.

---

## What changed, and why

### 1. It ran on one application

| v1 | v2 |
| --- | --- |
| `PROJECT_ROOT = r"D:\Sai\EnhanceUnitTesting"` in six modules and three mutmut configs | Every path derived from a workspace that defaults to a temp directory ([config.py](config.py)) |
| `APP_NAME = "InsuranceApp_Modified"` | Name read from the project's own `pyproject.toml` / `setup.cfg`, else the archive name ([ingest.py](ingest.py)) |
| Source at `source_files/<APP_NAME>`, tests at `tests/<APP_NAME>` | `src`/flat/package layouts detected; test roots found by looking for `test_*.py` and `*_test.py` |
| `unittest.defaultTestLoader.discover(top_level_dir=...)` | pytest, which also collects `unittest.TestCase` — a strict widening ([runner.py](runner.py)) |
| Test file found by converting `TestPolicyService` → `policy_service` and globbing | Test file read straight out of the pytest nodeid; source files taken from traceback frames ([resolver.py](resolver.py)) |
| Whatever is installed on the host | A virtualenv per project with its declared dependencies ([venv_manager.py](venv_manager.py)) |

The resolver change is the substantive one. v1's `locate_test_file` only worked
where the test class name encoded the filename, and when it failed it returned
`None` and the repair was silently skipped — so an unsupported project produced
"nothing to fix" rather than an error.

### 2. The repair step was generic

v1 sent every failure — broken import, wrong assertion, bad mock — to one
prompt (`build_fix_prompt`), wrote back whatever came out, and never checked
whether the test then passed.

v2 runs three agents:

- **Failure Analysis** ([agents/failure_analysis.py](agents/failure_analysis.py)) —
  a deterministic classifier over exception type, failing phase and message
  signatures, then the model confirms or corrects it with the traceback and
  source in view. If the model is unavailable or unsure, the heuristic stands.
- **Repair Strategy** ([agents/repair_strategy.py](agents/repair_strategy.py)) —
  each root cause has an ordered ladder of strategies ([strategies.py](strategies.py)),
  each with its own prompt and its own context budget. A strategy is never
  tried twice. When a ladder is exhausted the model picks a strategy from a
  different cause's ladder.
- **AutoFix** ([agents/autofix.py](agents/autofix.py)) — applies the patch via
  AST surgery, re-runs the test, and rolls the file back exactly if the repair
  does not hold.

Four things disqualify a repair: it does not parse, the test still fails, the
test passes only because assertions were weakened, or something that was
passing now fails.

Two behaviours worth naming:

- **Re-diagnosis.** If a repair turns `ModuleNotFoundError` into
  `AssertionError`, it worked and revealed the next problem. The loop
  re-diagnoses rather than escalating the import ladder ([orchestrator.py](orchestrator.py)).
- **Refusing to repair.** If the diagnosis is `production_bug` or
  `environment_dependency`, the test is left alone. Editing a test over a real
  source defect manufactures a false pass.

---

## Install and run

```bash
pip install -r requirements-autef2.txt
export OPENAI_API_KEY=sk-...        # or put a real key in OAI_CONFIG_LIST.json
```

Check whether a project is in scope — costs nothing, makes no model calls:

```bash
python -m autef2 check path/to/project
```

Diagnose and repair:

```bash
python -m autef2 run path/to/project.zip --venv --max-tests 20
```

Streamlit UI:

```bash
streamlit run src/autef2/ui.py
```

Run from `src/`, or install the package, so `autef2` is importable.

---

## Evaluation

```bash
python -m autef2 bench benchmarks/example_manifest.json --output results/
```

Writes `benchmark.md`, `benchmark.json`, and `observations.csv` (one row per
failing test per arm — the unit of analysis).

Both arms run on the **same** execution stack; only the repair architecture
differs. Running v1's original executor as the baseline would confound the two
claims, since a difference could then be explained by pytest collecting tests
unittest never found. The portability claim is measured separately, by how many
projects each version can process at all.

Controls: identical starting state (pristine copy restored between arms),
identical environment (one venv per project, shared), paired observations
(same failing tests offered to both arms), signature cache off by default.

Metrics: fix rate, attempts per fix, regression rate, cost per fix, and
**weakening rate** — how often a fix passed by gutting the assertion. The first
four can all be gamed by deleting assertions, so fix rate without weakening
rate is half a result.

Observations are paired, so the right significance test is McNemar's on the
fixed/not-fixed table, not a two-proportion z-test.

### Seeded faults

Real projects mostly ship green suites. `autef2 inject` seeds known faults —
one per root cause the ladders address — into tests that **currently pass**,
giving a sample that is large enough, stratified by construction, and with
known ground truth.

```bash
python -m autef2 inject path/to/project -n 20
```

Report seeded faults separately from naturally occurring failures. They are by
construction the kind of failure these repairs target.

---

## Scope

Python only. Projects whose suites run without external services. Databases,
network access, native extensions and non-standard build steps are out of
scope. The claim is broader applicability, not general applicability.

When a project is out of scope, `check` says so and `run` reports it as an
error rather than producing an empty result.

---

## Tests

```bash
python -m pytest tests_autef2 -q
```

48 tests, no API key needed — the model is scripted, everything else is real.

---

## Layout

```
config.py         run configuration; no hardcoded paths
models.py         dataclasses + the root-cause taxonomy
llm.py            OpenAI client with per-attempt cost accounting
ingest.py         archive -> ProjectLayout (name, roots, dependencies)
venv_manager.py   per-project virtualenv
runner.py         pytest execution and result parsing
_plugin/          stdlib-only pytest plugin injected into the target env
resolver.py       nodeid + traceback -> test file, source files
patcher.py        AST function surgery, validation, rollback
context.py        assembles prompt evidence per strategy
strategies.py     the repair ladders
guards.py         weakening and regression detection
cache.py          failure-signature -> winning strategy
agents/           the three agents
orchestrator.py   the repair loop
pipeline.py       end to end
eval/             baseline arm, fault injection, metrics, benchmark
cli.py, ui.py     entry points
```

---

## Note for this machine

TLS-intercepting antivirus breaks the OpenAI SDK's HTTPS connection. `llm.py`
detects the handshake failure and says so explicitly rather than reporting a
generic network error. Either disable HTTPS scanning or point `SSL_CERT_FILE` /
`REQUESTS_CA_BUNDLE` at the interceptor's root certificate.
