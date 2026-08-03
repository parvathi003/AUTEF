"""Run configuration.

The one rule this module exists to enforce: no path in AUTEF v2 is hardcoded.
v1 pinned ``D:\\Sai\\EnhanceUnitTesting`` into six modules and a mutmut config,
which is the single biggest reason it only ran on one machine against one app.
Everything here is derived from the workspace root, which itself defaults to a
temp directory and can be overridden per run.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: USD per token. gpt-4o-mini list pricing, kept here so cost-per-fix in the
#: evaluation can be recomputed when pricing changes without touching logic.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.150 / 1_000_000, "output": 0.600 / 1_000_000},
    "gpt-4o": {"input": 2.50 / 1_000_000, "output": 10.00 / 1_000_000},
    "gpt-4.1-mini": {"input": 0.40 / 1_000_000, "output": 1.60 / 1_000_000},
}

DEFAULT_MODEL = "gpt-4o-mini"


@dataclass
class AutefConfig:
    """Everything a run needs to know, with no machine-specific defaults."""

    # --- workspace -------------------------------------------------------
    #: Where projects are unpacked and worked on. A fresh temp dir per run
    #: unless the caller pins one (the evaluation harness pins one so runs
    #: can be inspected afterwards).
    workspace: Path = field(
        default_factory=lambda: Path(tempfile.mkdtemp(prefix="autef2_"))
    )

    # --- model -----------------------------------------------------------
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_output_tokens: int = 2048
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    # --- repair loop -----------------------------------------------------
    #: Rungs of the escalation ladder to try before giving up on a test.
    max_attempts: int = 3
    #: Reuse a previously successful strategy for an identical failure
    #: signature, skipping the analysis and strategy LLM calls.
    use_signature_cache: bool = True
    #: Refuse a repair that removes or weakens assertions.
    reject_weakened_fixes: bool = True
    #: Refuse a repair that breaks a previously passing test.
    reject_regressions: bool = True

    # --- execution -------------------------------------------------------
    #: Build a per-project virtualenv and install its declared dependencies.
    #: Correct but slow; off for quick local runs, on for the benchmark.
    use_venv: bool = False
    suite_timeout_s: int = 900
    single_test_timeout_s: int = 120
    install_timeout_s: int = 900
    #: Interpreter used to create project venvs.
    python_executable: str = field(default_factory=lambda: _default_python())

    # --- context budget --------------------------------------------------
    #: Truncation limits, in characters, for material sent to the model.
    max_source_chars: int = 12_000
    max_test_file_chars: int = 12_000
    max_traceback_chars: int = 4_000

    # --- misc ------------------------------------------------------------
    seed_marker: str = "AUTEF_INJECTED_FAULT"
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.api_key is None:
            self.api_key = resolve_api_key()

    # -- derived paths ----------------------------------------------------

    @property
    def projects_dir(self) -> Path:
        return self._ensure(self.workspace / "projects")

    @property
    def venvs_dir(self) -> Path:
        return self._ensure(self.workspace / "venvs")

    @property
    def reports_dir(self) -> Path:
        return self._ensure(self.workspace / "reports")

    @property
    def cache_path(self) -> Path:
        return self.workspace / "signature_cache.json"

    @property
    def pricing(self) -> Dict[str, float]:
        return MODEL_PRICING.get(self.model, MODEL_PRICING[DEFAULT_MODEL])

    def _ensure(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- construction -----------------------------------------------------

    @classmethod
    def from_env(cls, **overrides) -> "AutefConfig":
        """Build from environment, with keyword overrides winning.

        Recognised: AUTEF_WORKSPACE, AUTEF_MODEL, AUTEF_MAX_ATTEMPTS,
        AUTEF_USE_VENV, AUTEF_PYTHON, OPENAI_API_KEY, OPENAI_BASE_URL.
        """
        kwargs: Dict[str, object] = {}
        if os.getenv("AUTEF_WORKSPACE"):
            kwargs["workspace"] = Path(os.environ["AUTEF_WORKSPACE"])
        if os.getenv("AUTEF_MODEL"):
            kwargs["model"] = os.environ["AUTEF_MODEL"]
        if os.getenv("AUTEF_MAX_ATTEMPTS"):
            kwargs["max_attempts"] = int(os.environ["AUTEF_MAX_ATTEMPTS"])
        if os.getenv("AUTEF_USE_VENV"):
            kwargs["use_venv"] = _truthy(os.environ["AUTEF_USE_VENV"])
        if os.getenv("AUTEF_PYTHON"):
            kwargs["python_executable"] = os.environ["AUTEF_PYTHON"]
        if os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        kwargs.update(overrides)
        return cls(**kwargs)  # type: ignore[arg-type]


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_python() -> str:
    import sys

    return sys.executable or "python"


def load_dotenv_if_present() -> Optional[Path]:
    """Load a ``.env`` from the working directory or any parent.

    v1 called ``load_dotenv()`` at import time in four modules. Doing it here
    instead keeps the side effect in one place and out of import time, but it
    still has to happen -- without it a key in ``.env`` is silently invisible
    and the run fails claiming no key exists.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    for directory in [Path.cwd(), *Path.cwd().parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def resolve_api_key() -> Optional[str]:
    """Find the OpenAI key, honouring v1's OAI_CONFIG_LIST convention.

    Order: environment, then ``.env``, then ``OAI_CONFIG_LIST.json``.

    v1 shipped ``OAI_CONFIG_LIST.json`` holding the literal placeholder
    ``ENV_OPENAI_API_KEY`` and substituted the real key from the environment at
    import time. We keep reading that file so an existing checkout works
    unchanged, but the environment always wins and the placeholder is never
    mistaken for a key.
    """
    key = os.getenv("OPENAI_API_KEY")
    if not key or key.strip() in ("", "ENV_OPENAI_API_KEY"):
        load_dotenv_if_present()
        key = os.getenv("OPENAI_API_KEY")
    if key and key.strip() and key.strip() != "ENV_OPENAI_API_KEY":
        return key.strip()

    for candidate in _config_list_candidates():
        try:
            entries = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in entries if isinstance(entries, list) else []:
            value = (entry or {}).get("api_key", "")
            if value and value != "ENV_OPENAI_API_KEY":
                logger.info("Using API key from %s", candidate)
                return value
    return None


def _config_list_candidates() -> List[Path]:
    """Places OAI_CONFIG_LIST.json might live, nearest first."""
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "OAI_CONFIG_LIST.json",
        here.parent / "OAI_CONFIG_LIST.json",
        here.parent.parent / "agenticapp" / "OAI_CONFIG_LIST.json",
        here.parent.parent.parent / "src" / "agenticapp" / "OAI_CONFIG_LIST.json",
    ]
    if os.getenv("AUTEF_OAI_CONFIG"):
        candidates.insert(0, Path(os.environ["AUTEF_OAI_CONFIG"]))
    return [c for c in candidates if c.is_file()]


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
