# AUTEF v2 — installation and first run

For someone setting this up on a machine that has never run it. Takes about five
minutes, plus one decision about an API key.

---

## What you need first

| Requirement | Notes |
|---|---|
| **Python 3.9 or newer** | 3.11+ preferred. Check with `python --version`. |
| **An OpenAI API key** | Needed for stages 4–8 only. Stages 1–3 and 9 run without one, so you can verify the install for free. |
| **Internet access** | To download the packages, and to fetch a project by GitHub URL. |
| **~500 MB free disk** | The framework builds a throwaway virtual environment per project it analyses. |

No database, no Docker, no GPU. It is a plain Python application.

---

## Step 1 — Get the code

Unzip the delivered archive, or clone it, then open a terminal **inside the
project folder** — the one containing `src/`, `tests_autef2/` and
`requirements-autef2.txt`.

```
EnhanceUnitTesting-main/
├── src/
│   ├── autef2/        <- this project (v2)
│   └── agenticapp/    <- the original framework (v1), kept as the evaluation baseline
├── tests_autef2/
├── benchmarks/
└── requirements-autef2.txt
```

---

## Step 2 — Create a virtual environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
```

**macOS / Linux:**
```bash
python3 -m venv .venv
```

Everything below calls the interpreter by path, so you never need to "activate"
anything and can't accidentally install into system Python.

---

## Step 3 — Install the dependencies

**Windows:**
```powershell
.venv\Scripts\python.exe -m pip install -r requirements-autef2.txt
```

**macOS / Linux:**
```bash
.venv/bin/python -m pip install -r requirements-autef2.txt
```

That installs five packages: `openai`, `pytest`, `python-dotenv`, `streamlit`,
`pandas` (plus optional `pyyaml`).

> **Do not install `requirements.txt`.** That file belongs to v1 and pulls in
> langchain, transformers, autogen, mutmut and cosmic-ray. v2 needs none of
> them. The file is kept only so the original framework still runs as the
> comparison baseline.

---

## Step 4 — Provide the API key

Create a file named `.env` in the project root:

```
OPENAI_API_KEY=sk-your-key-here
```

Alternatives, in the order the framework looks:

1. the `OPENAI_API_KEY` environment variable
2. the `.env` file above
3. `src/agenticapp/OAI_CONFIG_LIST.json` (v1's format, still supported)

The default model is `gpt-4o-mini`. A full comparison run on a small repository
costs a fraction of a cent.

---

## Step 5 — Verify the install (no API key needed)

Two checks. Both are free.

**A. Run the test suite.** 138 tests, about 3–4 minutes.

```powershell
.venv\Scripts\python.exe -m pytest tests_autef2 -q
```

Expect `138 passed`. This exercises ingest, layout detection, the pytest runner,
the AST patcher, the mutation engine, the guards and the evaluation harness.

**B. Analyse the bundled sample project.**

Set the import path first — the package lives under `src/`:

```powershell
$env:PYTHONPATH="src"
```
```powershell
.venv\Scripts\python.exe -m autef2 check tests_autef2/sample_project
```

On macOS/Linux use `export PYTHONPATH=src` and `.venv/bin/python`.

You should see the detected layout and a suite result of **2 passed, 2 failed**
— the fixture ships two deliberately broken tests. If you see that, the install
is good.

---

## Step 6 — Run it

**The web application:**

```powershell
.venv\Scripts\python.exe -m streamlit run src/autef2/ui.py
```

It opens in a browser at `http://localhost:8501`. Three tabs: **Repair a
project**, **Compare v1 vs v2**, **Benchmark**.

In the Repair tab: choose a source (upload a `.zip`, paste a GitHub URL, or give
a folder path), then click the nine stage buttons in order, or **Run all
stages**.

**Or from the command line:**

| Goal | Command (after `$env:PYTHONPATH="src"`) |
|---|---|
| Is this project in scope? (free) | `... -m autef2 check <project>` |
| Full nine-stage run | `... -m autef2 run <project> --all-phases --venv` |
| Compare v1 against v2 | `... -m autef2 compare <project> -n 8` |
| Measure across many projects | `... -m autef2 bench benchmarks/manifest.json` |
| Seed known faults only | `... -m autef2 inject <project> -n 10` |

`<project>` can be a GitHub URL, a `.zip`, or a directory.

---

## Recommended first real project

```powershell
.venv\Scripts\python.exe -m autef2 compare https://github.com/astanin/python-tabulate -n 8
```

`python-tabulate` processes in about 8 seconds with 322 tests and needs no
dependency installation. `cachetools` is equally quick.

`-n 8` seeds eight known faults into currently-passing tests first — real
repositories mostly pass, so without seeding there is nothing for either version
to repair and nothing to compare.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No module named 'autef2'` | `PYTHONPATH` is not set. Run `$env:PYTHONPATH="src"` (PowerShell) or `export PYTHONPATH=src` (bash) from the project root. |
| `No OpenAI API key found` | No `.env`, or it's not in the project root. Stages 1–3 and 9 still work without one. |
| SSL / certificate errors on any model call | Antivirus TLS interception (Kaspersky and similar) breaks the OpenAI client. `llm.py` detects this and says so. Disable HTTPS scanning, or add the corporate root certificate to the environment. |
| Stage 3 reports "the suite was still running and was stopped" | The project's own test suite is too large or too slow. Check its size first with `pytest tests --collect-only -q`. Anything over a few thousand tests is impractical — the pipeline runs the suite several times. |
| Every test fails with `ModuleNotFoundError` | The project's dependencies are not installed. Add `--venv` on the CLI, or tick **Isolated virtualenv per project** in the app sidebar. |
| venv creation fails on Windows with a path-length error | The workspace path is too deep. Pass a short one: `--workspace C:\autef`. |
| "The test suite could not be executed, so this project is out of scope" | Correct behaviour. The scope boundary is *the project's suite already runs under pytest*. A project needing a database, network services, native extensions or a custom build step is out of scope by design. |

---

## A note on the current packaging

The project runs from source via `PYTHONPATH=src`. There is no
`pip install autef2` and no `autef2` console command, because `pyproject.toml`
and `setup.cfg` in this repository still hold the *original* framework's
mutation-testing configuration rather than packaging metadata.

This does not affect behaviour — it only means the two-step invocation above.
