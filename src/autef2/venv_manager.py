"""Per-project virtual environments.

Every project gets its own interpreter with its own declared dependencies, so
one project's pinned version of a library cannot decide whether another
project's tests pass. This is what makes running more than one project on the
same machine meaningful.

Installation is best-effort by design. A project whose dependencies will not
resolve is out of the stated scope, but it should degrade to "ran without
isolation, here is why" rather than crash the run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import AutefConfig
from .models import ProjectLayout

logger = logging.getLogger(__name__)

#: Installed into every environment: the runner needs pytest, and the reporting
#: plugin is standard-library only so nothing else is required.
RUNNER_REQUIREMENTS = ["pytest>=7.0"]

#: A requirement line naming pytest itself, e.g. ``pytest==8.3.3`` or
#: ``pytest >= 7, < 9``. Deliberately does not match ``pytest-django``.
_PYTEST_REQUIREMENT_RE = re.compile(
    r"^\s*pytest\s*(?P<spec>(?:[=<>!~]=?[^;#,]+)(?:\s*,\s*[=<>!~]=?[^;#,]+)*)?\s*"
    r"(?:;.*)?$",
    re.IGNORECASE,
)


@dataclass
class Environment:
    """An interpreter the runner can execute tests with."""

    python: str
    isolated: bool
    root: Optional[str] = None
    installed: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "python": self.python,
            "isolated": self.isolated,
            "root": self.root,
            "installed": self.installed,
            "warnings": self.warnings,
        }


def prepare_environment(
    layout: ProjectLayout, config: AutefConfig
) -> Environment:
    """Build (or reuse) an environment able to run this project's tests."""
    if not config.use_venv:
        env = Environment(python=sys.executable, isolated=False)
        if not _has_pytest(sys.executable):
            env.warnings.append(
                "pytest is not importable by the current interpreter; "
                "install it or run with --venv."
            )
        return env

    venv_root = Path(config.venvs_dir) / _safe_name(layout.name)
    python = _venv_python(venv_root)

    if not python.exists():
        logger.info("Creating virtualenv for %s at %s", layout.name, venv_root)
        try:
            venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_root))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create virtualenv: %s", exc)
            warnings = [f"virtualenv creation failed: {exc}"]
            if os.name == "nt" and len(str(venv_root)) > 120:
                # ensurepip writes deep into site-packages and does not use
                # Windows' extended-length paths, so a deep workspace defeats it
                # however short the venv's own directory name is.
                warnings.append(
                    f"The virtualenv path is {len(str(venv_root))} characters. "
                    "On Windows, venv creation fails once the paths inside "
                    "site-packages pass 260 characters -- use a shorter "
                    "--workspace."
                )
            return Environment(
                python=sys.executable, isolated=False, warnings=warnings
            )

    env = Environment(python=str(python), isolated=True, root=str(venv_root))

    runner_requirement = pinned_pytest(layout) or RUNNER_REQUIREMENTS[0]
    if runner_requirement != RUNNER_REQUIREMENTS[0]:
        logger.info("Using the project's pinned pytest: %s", runner_requirement)
    if not _install(env, [runner_requirement], config, label="runner"):
        # A pin we cannot resolve must not leave the environment without pytest.
        _install(env, RUNNER_REQUIREMENTS, config, label="runner (fallback)")

    _install_project_dependencies(env, layout, config)

    if not _has_pytest(env.python):
        env.warnings.append(
            "pytest missing from the project virtualenv; falling back to the "
            "host interpreter."
        )
        return Environment(
            python=sys.executable,
            isolated=False,
            warnings=env.warnings,
        )
    return env


def install_requirement(
    env: Environment, requirement: str, config: AutefConfig
) -> bool:
    """Add one package to an existing environment.

    Used for tooling a phase needs only when that phase runs -- coverage, for
    instance -- so an ordinary repair run does not pay to install it.
    """
    return _install(env, [requirement], config, label=requirement)


def pinned_pytest(layout: ProjectLayout) -> Optional[str]:
    """The pytest requirement this project asks for, if it names one.

    Installing the newest pytest into every environment looks harmless and is
    not: Flask's conftest uses ``_pytest.monkeypatch.notset``, a private name
    removed in pytest 9, so the whole suite errors out under a pytest the
    project never claimed to support. The failure looks like a broken project
    and is really a broken environment.

    Only the version is honoured. The reporting plugin uses hooks that have been
    stable for many major versions, so an older pytest still reports normally.
    """
    for requirement in _dependency_candidates(layout):
        match = _PYTEST_REQUIREMENT_RE.match(requirement)
        if match:
            spec = (match.group("spec") or "").strip()
            if spec:
                return f"pytest{spec}"
            # A bare "pytest" is not a pin; keep looking for one that is.

    # No stated constraint. A lockfile records the version the project's own CI
    # actually runs, which is the next best evidence -- and often the only one.
    # Flask declares a bare "pytest" but locks 9.0.3, and its conftest uses a
    # private name that pytest 9.1 removed.
    return _lockfile_pytest(Path(layout.root))


def _lockfile_pytest(root: Path) -> Optional[str]:
    """The exact pytest version a lockfile records, as a pip specifier."""
    from .ingest import _load_toml

    for name in ("uv.lock", "poetry.lock"):
        path = root / name
        if not path.is_file():
            continue
        data = _load_toml(path) or {}
        packages = data.get("package")
        if not isinstance(packages, list):
            continue
        for package in packages:
            if (
                isinstance(package, dict)
                and str(package.get("name", "")).lower() == "pytest"
                and package.get("version")
            ):
                return f"pytest=={package['version']}"

    pipfile = root / "Pipfile.lock"
    if pipfile.is_file():
        try:
            data = json.loads(pipfile.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return None
        for section in ("develop", "default"):
            entry = (data.get(section) or {}).get("pytest")
            if isinstance(entry, dict) and isinstance(entry.get("version"), str):
                version = entry["version"]
                return f"pytest{version}" if version.startswith("=") else f"pytest=={version}"
    return None


def _dependency_candidates(layout: ProjectLayout) -> List[str]:
    """Every requirement string the project declares, test extras included.

    ``declared_dependencies`` covers requirements files and runtime metadata.
    Test tooling usually lives somewhere else -- an optional-dependencies extra,
    a PEP 735 dependency group, or a poetry dev group -- so those are read too.
    """
    candidates: List[str] = list(layout.declared_dependencies)

    pyproject = Path(layout.root) / "pyproject.toml"
    if not pyproject.is_file():
        return candidates

    from .ingest import _load_toml

    data = _load_toml(pyproject) or {}
    project = data.get("project")
    if isinstance(project, dict):
        extras = project.get("optional-dependencies")
        if isinstance(extras, dict):
            for group in extras.values():
                if isinstance(group, list):
                    candidates.extend(str(item) for item in group)

    groups = data.get("dependency-groups")  # PEP 735
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                candidates.extend(str(item) for item in group if isinstance(item, str))

    poetry_groups = (
        data.get("tool", {}).get("poetry", {}).get("group", {})
        if isinstance(data.get("tool"), dict)
        else {}
    )
    if isinstance(poetry_groups, dict):
        for group in poetry_groups.values():
            dependencies = group.get("dependencies") if isinstance(group, dict) else None
            if isinstance(dependencies, dict):
                candidates.extend(
                    f"{name}{spec if isinstance(spec, str) else ''}"
                    for name, spec in dependencies.items()
                )

    return candidates


def _install_project_dependencies(
    env: Environment, layout: ProjectLayout, config: AutefConfig
) -> None:
    root = Path(layout.root)

    requirement_files = [
        Path(f) for f in layout.dependency_files if _is_test_requirements(Path(f))
    ]
    for requirements in requirement_files:
        _install(
            env,
            ["-r", str(requirements)],
            config,
            label=f"requirements ({requirements.name})",
        )

    if layout.installable:
        # Editable install so the tests import the working tree we are about
        # to patch, not a copy taken at install time.
        _install(env, ["-e", str(root)], config, label="project (editable)")
    elif not requirement_files and layout.declared_dependencies:
        _install(
            env, list(layout.declared_dependencies), config, label="declared deps"
        )


#: Stems worth installing from a ``requirements/`` directory. Projects that
#: split their pins that way (Flask does) put the test tooling in one of these;
#: ``docs.txt`` and ``typing.txt`` would drag in sphinx and mypy for nothing.
_TEST_REQUIREMENT_STEMS = {"tests", "test", "dev", "testing", "ci"}


def _is_test_requirements(path: Path) -> bool:
    if path.name.startswith("requirements"):
        return True
    return path.parent.name == "requirements" and path.stem in _TEST_REQUIREMENT_STEMS


def _install(
    env: Environment, args: List[str], config: AutefConfig, *, label: str
) -> bool:
    command = [env.python, "-m", "pip", "install", "--disable-pip-version-check", *args]
    logger.info("Installing %s into %s", label, env.root or "host")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.install_timeout_s,
            check=False,
            # pip can ask for input (a keyring prompt on a private index, most
            # often). Unattended, that is a hang, not a question.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        env.warnings.append(f"install timed out: {label}")
        return False
    except OSError as exc:
        env.warnings.append(f"install could not start ({label}): {exc}")
        return False

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-5:]
        env.warnings.append(f"install failed ({label}): {' | '.join(tail)}")
        logger.warning("pip install failed for %s", label)
        return False

    env.installed.append(label)
    return True


def _venv_python(venv_root: Path) -> Path:
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _has_pytest(python: str) -> bool:
    try:
        completed = subprocess.run(
            [python, "-c", "import pytest"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:60]
