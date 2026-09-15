"""Work out what an uploaded project actually is.

v1 assumed the answer: ``APP_NAME = "InsuranceApp_Modified"``, source under
``source_files/<APP_NAME>``, tests under ``tests/<APP_NAME>``. Upload anything
else and the paths simply did not exist.

Here nothing is assumed. The archive is unpacked, the project name comes from
its own packaging metadata, and the test roots, import roots and dependency
declarations are discovered by looking at the tree.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePath
from typing import IO, Iterable, List, Optional, Sequence, Set, Union

from .config import AutefConfig
from .models import ProjectLayout

logger = logging.getLogger(__name__)

#: Directories that are never project source, however deep they appear.
EXCLUDED_DIRS: Set[str] = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "node_modules", "build", "dist",
    "site-packages", ".idea", ".vscode", ".eggs", "htmlcov", ".autef",
}

#: Virtualenv directory names, excluded so a committed venv is never mistaken
#: for project code.
VENV_DIR_NAMES: Set[str] = {"venv", ".venv", "env", ".env", "virtualenv", ".autef_venv"}

#: pytest's own default patterns are ``test_*.py`` and ``*_test.py``. Two more
#: are recognised here because whole ecosystems use them and would otherwise
#: report zero tests: Django's per-app ``tests.py`` (what ``manage.py test``
#: collects) and the single-file ``test.py`` habit. When a project turns out to
#: use one of these, the runner widens pytest's ``python_files`` to match --
#: discovering the file is useless if pytest then refuses to collect it.
TEST_FILE_RE = re.compile(r"^(test_.+|.+_test|tests?)\.py$")

#: The extra basenames above, as pytest ``python_files`` globs.
NON_STANDARD_TEST_FILES = ("tests.py", "test.py")

#: Windows rejects paths beyond 260 characters unless they carry this prefix.
_LONG_PATH_PREFIX = "\\\\?\\"
_LONG_PATH_LIMIT = 240


class IngestError(RuntimeError):
    pass


def ingest(
    source: Union[str, Path, IO[bytes]],
    config: AutefConfig,
    *,
    name_hint: Optional[str] = None,
) -> ProjectLayout:
    """Materialise a project into the workspace and describe it.

    ``source`` may be

    * a directory,
    * a path to a ``.zip`` or ``.tar.gz``/``.tgz``/``.tar.bz2`` archive,
    * a repository or archive URL (``https://github.com/owner/repo``, with an
      optional ``/tree/<branch>``, or any direct archive link),
    * or an open binary stream, for an upload held in memory.
    """
    root = _materialise(source, config, name_hint)
    return analyse(root, name_hint=name_hint)


# ---------------------------------------------------------------------------
# unpacking
# ---------------------------------------------------------------------------


def _materialise(
    source: Union[str, Path, IO[bytes]],
    config: AutefConfig,
    name_hint: Optional[str],
) -> Path:
    if hasattr(source, "read"):
        return _unpack_stream(source, config, name_hint)  # type: ignore[arg-type]

    if isinstance(source, str) and _is_url(source):
        source = fetch_url(source, config)

    path = Path(source).expanduser().resolve()
    if path.is_dir():
        target = config.projects_dir / _bare_name(name_hint, path)
        # Work in place only for a project that already lives in the workspace.
        # Everything AUTEF does next -- seeding faults, patching tests -- writes
        # to this directory, so it must never be the caller's own copy. Comparing
        # the two paths is not enough on its own: a name_hint that is itself an
        # absolute path makes the join collapse onto the original.
        if target.resolve() == path and _is_within(path, config.projects_dir.resolve()):
            return path
        if target.exists():
            shutil.rmtree(_native(target), onerror=_force_remove)
        try:
            shutil.copytree(_native(path), _native(target), ignore=_copy_ignore)
        except OSError as exc:
            raise IngestError(_path_error(target, exc)) from exc
        return target
    if path.suffix.lower() == ".zip":
        with path.open("rb") as handle:
            return _unpack_stream(handle, config, name_hint or path.stem)
    if _is_tarball(path):
        return _unpack_tar(path, config, name_hint or _tar_stem(path))
    raise IngestError(f"Not a directory, .zip or .tar.gz archive: {path}")


def _unpack_stream(
    stream: IO[bytes], config: AutefConfig, name_hint: Optional[str]
) -> Path:
    target = config.projects_dir / (name_hint or "uploaded_project")
    if target.exists():
        shutil.rmtree(_native(target), onerror=_force_remove)
    target.mkdir(parents=True)

    with zipfile.ZipFile(stream) as archive:
        for member in archive.infolist():
            destination = _safe_join(target, member.filename)
            if destination is None:
                logger.warning("Skipping unsafe archive path: %s", member.filename)
                continue
            try:
                if member.is_dir():
                    _native(destination).mkdir(parents=True, exist_ok=True)
                    continue
                _native(destination.parent).mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src:
                    with _native(destination).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            except OSError as exc:
                raise IngestError(_path_error(destination, exc)) from exc

    return _strip_wrapper_dir(target)


def _unpack_tar(path: Path, config: AutefConfig, name_hint: str) -> Path:
    """Extract a source tarball. PyPI sdists are distributed this way."""
    target = config.projects_dir / name_hint
    if target.exists():
        shutil.rmtree(_native(target), onerror=_force_remove)
    target.mkdir(parents=True)

    native = _native(target)
    try:
        with tarfile.open(path) as archive:
            try:
                archive.extractall(native, filter="data")  # 3.12+: blocks traversal
            except TypeError:  # pragma: no cover - older interpreters
                for member in archive.getmembers():
                    if _safe_join(target, member.name) is None:
                        logger.warning("Skipping unsafe archive path: %s", member.name)
                        continue
                    archive.extract(member, native)
    except OSError as exc:
        raise IngestError(_path_error(target, exc)) from exc

    return _strip_wrapper_dir(target)


def _native(path: Path) -> Path:
    """The form of ``path`` the OS will actually accept.

    On Windows a path over 260 characters fails with a bare
    ``FileNotFoundError`` unless it carries the extended-length prefix. Real
    projects hit this easily: django-crispy-forms ships fixtures nested at
    ``tests/results/bootstrap/test_layout_objects/...``, which busts the limit
    as soon as the workspace itself sits a few directories down.

    The prefix is applied to every path rather than only to long ones, because
    the operations it is used for recurse: a directory whose own name fits can
    still contain a file whose full path does not, and ``shutil.rmtree`` builds
    those child paths from whatever it was handed.
    """
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith(_LONG_PATH_PREFIX):
        return path
    absolute = path if path.is_absolute() else path.resolve()
    text = str(absolute)
    if text.startswith("\\\\"):  # UNC share: \\server\share -> \\?\UNC\server\share
        return Path(_LONG_PATH_PREFIX + "UNC" + text[1:])
    return Path(_LONG_PATH_PREFIX + text)


def _path_error(destination: Path, exc: OSError) -> str:
    if os.name == "nt" and len(str(destination)) >= _LONG_PATH_LIMIT:
        return (
            f"Could not write {destination.name}: the full path is "
            f"{len(str(destination))} characters, past what Windows accepts. "
            "Use a shorter workspace directory (the --workspace option), or "
            "enable long paths in Windows. Original error: " + str(exc)
        )
    return f"Could not unpack the archive: {exc}"


# ---------------------------------------------------------------------------
# remote sources
# ---------------------------------------------------------------------------

_GITHUB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    # A branch name may itself contain slashes ("feature/x"), so the ref runs
    # to the end of the path rather than to the next separator.
    r"(?:/tree/(?P<ref>[^?#]+?))?/?(?:[?#].*)?$"
)

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar")

#: Downloads are capped so a mistyped link cannot fill the disk.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "git@"))


def _is_tarball(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar"))


def _tar_stem(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tgz", ".tar.bz2", ".tar"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _looks_like_sha(ref: str) -> bool:
    """Is this a commit id rather than a branch name?

    Seven hex characters is git's own abbreviation floor. A branch could in
    principle be named "abcdef1", but pinning is the deliberate act and a
    branch with a hex-only name is not.
    """
    return 7 <= len(ref) <= 40 and all(c in "0123456789abcdefABCDEF" for c in ref)


def fetch_url(url: str, config: AutefConfig) -> Path:
    """Download a project from a URL and return the local archive path.

    A GitHub project page is turned into its archive link rather than cloned,
    so no git installation is required and only the tip of one branch is
    transferred. Anything that is already an archive link is taken as it is.
    Other git URLs fall back to a shallow clone.
    """
    url = url.strip()
    downloads = Path(config.workspace) / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    match = _GITHUB_RE.match(url)
    if match:
        owner, repo = match.group("owner"), match.group("repo")
        ref = match.group("ref")
        if not ref:
            archive = f"https://codeload.github.com/{owner}/{repo}/zip/HEAD"
        elif _looks_like_sha(ref):
            # A commit is not under refs/heads, and asking for it there 404s.
            # Pinning to a commit is the only way a benchmark number stays
            # reproducible: a branch moves, and tabulate went from 322 tests to
            # 306 between two runs of this benchmark.
            archive = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
        else:
            archive = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
        label = f"{repo}-{ref}" if ref else repo
        return _download(archive, downloads / f"{_safe_name(label)}.zip")

    if url.lower().endswith(ARCHIVE_SUFFIXES):
        name = _safe_name(url.rsplit("/", 1)[-1]) or "download.zip"
        return _download(url, downloads / name)

    if url.startswith("git@") or url.endswith(".git"):
        return _clone(url, config)

    raise IngestError(
        "Give a GitHub project URL (https://github.com/owner/repo), a direct "
        f"link to a .zip or .tar.gz, or a .git URL. Got: {url}"
    )


def _download(url: str, destination: Path) -> Path:
    import urllib.error
    import urllib.request

    logger.info("Downloading %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": "autef2"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_DOWNLOAD_BYTES:
                raise IngestError(
                    f"Archive is {declared / 1e6:.0f} MB, over the "
                    f"{MAX_DOWNLOAD_BYTES / 1e6:.0f} MB limit."
                )
            written = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        handle.close()
                        destination.unlink(missing_ok=True)
                        raise IngestError(
                            "Archive exceeds the "
                            f"{MAX_DOWNLOAD_BYTES / 1e6:.0f} MB download limit."
                        )
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        hint = (
            " The default branch may be named differently, or the repository "
            "may be private; try the /tree/<branch> URL."
            if exc.code == 404
            else ""
        )
        raise IngestError(f"Download failed ({exc.code} {exc.reason}).{hint}") from exc
    except urllib.error.URLError as exc:
        raise IngestError(f"Download failed: {exc.reason}") from exc

    logger.info("Downloaded %.1f MB to %s", written / 1e6, destination.name)
    return destination


def _clone(url: str, config: AutefConfig) -> Path:
    """Shallow-clone a git URL. Used only for non-GitHub git remotes."""
    import subprocess

    git = shutil.which("git")
    if git is None:
        raise IngestError(f"git is not installed, so {url} cannot be cloned.")

    name = _safe_name(url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
    target = config.projects_dir / (name or "cloned_project")
    if target.exists():
        shutil.rmtree(target, onerror=_force_remove)

    logger.info("Cloning %s", url)
    completed = subprocess.run(
        [git, "clone", "--depth", "1", url, str(target)],
        capture_output=True, text=True, errors="replace", timeout=600, check=False,
        # A private repo would otherwise prompt for credentials and sit there
        # for the full ten minutes. Fail with git's own message instead.
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise IngestError(
            f"git clone failed: {(completed.stderr or completed.stdout).strip()[-300:]}"
        )
    return target


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80]


def _bare_name(name_hint: Optional[str], path: Path) -> str:
    """A single directory name, never a path.

    ``config.projects_dir / name_hint`` silently discards the workspace if
    ``name_hint`` is absolute, so a hint carrying separators or a drive is
    reduced to its last component rather than trusted.
    """
    if not name_hint:
        return path.name
    candidate = PurePath(str(name_hint).replace("\\", "/")).name
    if candidate != str(name_hint):
        logger.debug("Reduced project name %r to %r", name_hint, candidate)
    return candidate or path.name


def _safe_join(base: Path, member_name: str) -> Optional[Path]:
    """Reject absolute paths and ``..`` traversal in archive members."""
    candidate = (base / member_name).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate


def _strip_wrapper_dir(target: Path) -> Path:
    """Collapse the single top-level folder GitHub archives wrap projects in."""
    entries = [e for e in target.iterdir() if e.name not in {"__MACOSX"}]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return target


def _copy_ignore(directory: str, names: Sequence[str]) -> Set[str]:
    return {
        n
        for n in names
        if n in EXCLUDED_DIRS or n in VENV_DIR_NAMES or n.endswith(".egg-info")
    }


def _force_remove(func, path, _exc_info):
    """shutil.rmtree onerror hook: clear the read-only bit and retry."""
    os.chmod(path, 0o700)
    func(path)


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def analyse(root: Union[str, Path], *, name_hint: Optional[str] = None) -> ProjectLayout:
    """Describe an already-unpacked project."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise IngestError(f"Project root does not exist: {root}")

    notes: List[str] = []
    name = name_hint or _detect_name(root) or root.name

    test_files = sorted(str(p) for p in _walk_python(root) if TEST_FILE_RE.match(p.name))
    if not test_files:
        notes.append(
            "No files matching test_*.py, *_test.py, tests.py or test.py were found."
        )
    elif any(Path(p).name in NON_STANDARD_TEST_FILES for p in test_files):
        notes.append(
            "Found tests.py/test.py style tests, which pytest does not collect "
            "by default; python_files will be widened for this project."
        )

    test_roots = _minimal_roots(root, [Path(p).parent for p in test_files])
    source_roots, layout_style = _detect_source_roots(root, test_roots)
    import_roots = _detect_import_roots(root, source_roots, test_roots, notes)

    dependency_files, declared = _detect_dependencies(root)
    installable = (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()

    layout = ProjectLayout(
        name=name,
        root=str(root),
        import_roots=[str(p) for p in import_roots],
        test_roots=[str(p) for p in test_roots],
        source_roots=[str(p) for p in source_roots],
        test_files=test_files,
        dependency_files=[str(p) for p in dependency_files],
        declared_dependencies=declared,
        installable=installable,
        layout_style=layout_style,
        notes=notes,
    )
    logger.info(
        "Ingested %s: %d test files, %d test roots, layout=%s, installable=%s",
        name, len(test_files), len(test_roots), layout_style, installable,
    )
    return layout


def _walk_python(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in EXCLUDED_DIRS
            and d not in VENV_DIR_NAMES
            and not d.endswith(".egg-info")
        ]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def _detect_name(root: Path) -> Optional[str]:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = _load_toml(pyproject)
        if data:
            for path in (("project", "name"), ("tool", "poetry", "name")):
                value = _dig(data, path)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        # Fall back to a regex when tomllib is unavailable or parsing failed.
        match = re.search(
            r'^\s*name\s*=\s*["\']([^"\']+)["\']',
            pyproject.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            value = parser.get("metadata", "name", fallback="").strip()
            if value:
                return value
        except configparser.Error:
            pass
    return None


def _detect_source_roots(root: Path, test_roots: List[Path]) -> tuple[List[Path], str]:
    """Where the code under test lives, and which conventional layout it is."""
    src = root / "src"
    if src.is_dir() and any(_walk_python(src)):
        return [src], "src"

    packages = [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_dir()
        and entry.name not in EXCLUDED_DIRS
        and entry.name not in VENV_DIR_NAMES
        and entry not in test_roots
        and (entry / "__init__.py").is_file()
    ]
    if packages:
        return packages, "package"
    return [root], "flat"


def _detect_import_roots(
    root: Path,
    source_roots: List[Path],
    test_roots: List[Path],
    notes: List[str],
) -> List[Path]:
    """Directories to prepend to sys.path so the tests can import the code.

    A ``src`` layout needs ``src`` on the path; a flat layout needs the project
    root. Test directories without ``__init__.py`` are rootdir-relative under
    pytest, so we add their parent too rather than relying on any one
    convention holding.

    A directory that is itself a package is never added. Putting ``pyparsing/``
    on the path does not help ``import pyparsing`` -- the project root already
    does that -- but it does make every module inside it importable as a
    top-level name, so ``pyparsing/warnings.py`` shadows the standard library's
    ``warnings`` and the whole suite dies during collection. The same killed
    jmespath, whose ``ast.py`` shadows ``ast``. Measured on both: 0 tests
    collected before this, the full suite after.
    """
    roots: List[Path] = [root]
    for source_root in source_roots:
        if source_root == root:
            continue
        if (source_root / "__init__.py").is_file():
            parent = source_root.parent
            if parent not in roots:
                roots.append(parent)
            notes.append(
                f"{source_root.name} is a package, so its parent is on the "
                "path rather than the package itself"
            )
            continue
        roots.append(source_root)
    for test_root in test_roots:
        if not (test_root / "__init__.py").is_file() and test_root != root:
            roots.append(test_root)
    if len(roots) > 1:
        notes.append(f"Import roots inferred: {[str(r) for r in roots]}")
    return _dedupe(roots)


def _minimal_roots(root: Path, directories: Iterable[Path]) -> List[Path]:
    """Collapse a set of directories to their top-most distinct ancestors."""
    unique = _dedupe(sorted({d.resolve() for d in directories}, key=lambda p: len(p.parts)))
    minimal: List[Path] = []
    for directory in unique:
        if not any(_is_within(directory, kept) for kept in minimal):
            minimal.append(directory)
    return minimal or [root]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _dedupe(items: Iterable[Path]) -> List[Path]:
    seen: Set[Path] = set()
    result: List[Path] = []
    for item in items:
        resolved = item.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _detect_dependencies(root: Path) -> tuple[List[Path], List[str]]:
    """Collect declared runtime dependencies from the usual places."""
    files: List[Path] = []
    deps: List[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        files.append(pyproject)
        data = _load_toml(pyproject)
        if data:
            project_deps = _dig(data, ("project", "dependencies"))
            if isinstance(project_deps, list):
                deps.extend(str(d) for d in project_deps)
            poetry_deps = _dig(data, ("tool", "poetry", "dependencies"))
            if isinstance(poetry_deps, dict):
                deps.extend(
                    name for name in poetry_deps if name.lower() != "python"
                )

    for pattern in ("requirements.txt", "requirements*.txt", "requirements/*.txt"):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file() and candidate not in files:
                files.append(candidate)
                deps.extend(_parse_requirements(candidate))

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        files.append(setup_cfg)
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
            raw = parser.get("options", "install_requires", fallback="")
            deps.extend(line.strip() for line in raw.splitlines() if line.strip())
        except configparser.Error:
            pass

    return files, sorted({d for d in deps if d and not d.startswith("-")})


def _parse_requirements(path: Path) -> List[str]:
    lines: List[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _load_toml(path: Path) -> Optional[dict]:
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - older interpreters
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - malformed pyproject is not fatal
        return None


def _dig(data: dict, path: Sequence[str]):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node
