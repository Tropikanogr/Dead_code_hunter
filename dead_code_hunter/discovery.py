"""Repository walking and project discovery.

Projects are discovered independently per language:

* Python: marker files define project boundaries, but *every* directory that
  contains ``.py`` files is scanned (spec PY-4).
* JS/TS: every ``package.json`` outside dependency trees is a package; a root
  that declares workspaces is treated as one monorepo unit (spec JS-4).
* Dart: every ``pubspec.yaml`` is a package.

Discovery never follows symlinks and never enters secret or dependency trees.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .models import ProjectInfo, normalize_path
from .safety import SKIP_DIRS, is_secret_path

PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "tox.ini")
JS_MARKERS = ("package.json",)
DART_MARKERS = ("pubspec.yaml",)
WORKSPACE_FILES = ("pnpm-workspace.yaml", "pnpm-workspace.yml", "lerna.json", "nx.json", "turbo.json")

SOURCE_SUFFIXES = {
    "python": (".py",),
    "js": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"),
    "dart": (".dart",),
}


@dataclass
class RepoScan:
    root: Path
    files: list[str] = field(default_factory=list)  # repo-relative POSIX paths
    project_files: dict[str, list[str]] = field(default_factory=dict)
    truncated: bool = False
    skipped_secret_files: int = 0
    skipped_links: int = 0
    project_limit_reached: bool = False


def iter_files(root: Path, max_files: int = 200_000) -> tuple[list[str], dict[str, Any]]:
    """Walk the repository without following links or entering skipped trees."""
    files: list[str] = []
    stats = {"truncated": False, "skipped_secret_files": 0, "skipped_links": 0}
    for where, dirs, names in os.walk(root, followlinks=False):
        parent = Path(where)
        kept: list[str] = []
        for name in sorted(dirs):
            candidate = parent / name
            if name in SKIP_DIRS or is_secret_path(name):
                stats["skipped_secret_files"] += 1 if is_secret_path(name) else 0
                continue
            if candidate.is_symlink():
                stats["skipped_links"] += 1
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            if is_secret_path(name):
                stats["skipped_secret_files"] += 1
                continue
            candidate = parent / name
            if candidate.is_symlink():
                stats["skipped_links"] += 1
                continue
            if not candidate.is_file():
                continue
            files.append(candidate.relative_to(root).as_posix())
            if len(files) >= max_files:
                stats["truncated"] = True
                return files, stats
    return files, stats


def _dir_has_suffix(files: list[str], directory: str, suffixes: tuple[str, ...]) -> bool:
    prefix = "" if directory in ("", ".") else f"{directory}/"
    for path in files:
        if prefix and not path.startswith(prefix):
            continue
        remainder = path[len(prefix):]
        if "/" not in remainder and remainder.endswith(suffixes):
            return True
    return False


@dataclass
class PackageJson:
    path: str  # repo-relative package.json path
    dir: str  # repo-relative directory ("" for repo root)
    data: dict[str, Any]
    error: str | None = None

    @property
    def name(self) -> str | None:
        value = self.data.get("name")
        return value if isinstance(value, str) else None

    @property
    def private(self) -> bool:
        return self.data.get("private") is True


def load_package_json(root: Path, rel_path: str) -> PackageJson:
    abs_path = root / rel_path
    directory = normalize_path(os.path.dirname(rel_path).replace("\\", "/"))
    if directory == "":
        directory = "."
    try:
        data = json.loads(abs_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return PackageJson(rel_path, directory, {}, error="package.json is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return PackageJson(rel_path, directory, {}, error=str(exc))
    return PackageJson(rel_path, directory, data)


def _declares_workspaces(root: Path, pkg: PackageJson) -> bool:
    workspaces = pkg.data.get("workspaces")
    if isinstance(workspaces, list) and workspaces:
        return True
    if isinstance(workspaces, dict) and workspaces.get("packages"):
        return True
    return any((root / name).is_file() for name in WORKSPACE_FILES)


@dataclass
class Discovery:
    projects: list[ProjectInfo]
    packages: list[PackageJson]
    monorepo_root: str | None
    root: Path | None = None
    all_files: list[str] = field(default_factory=list)
    truncated: bool = False
    project_limit_reached: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    file_index: dict[str, list[str]] = field(default_factory=dict)

    def project_paths(self) -> list[str]:
        return [p.path for p in self.projects]

    def owner(self, file: str) -> str:
        """Deepest project path that owns ``file`` (``.`` when none)."""
        best = "."
        best_len = -1
        for project in self.projects:
            base = project.path
            if base == ".":
                candidate_len = 0
                matches = True
            else:
                matches = file == base or file.startswith(f"{base}/")
                candidate_len = len(base)
            if matches and candidate_len > best_len:
                best, best_len = base, candidate_len
        return best


def discover(root: Path, max_projects: int = 64) -> Discovery:
    root = root.resolve()
    files, stats = iter_files(root)
    projects: list[ProjectInfo] = []

    # --- Python -------------------------------------------------------------
    py_files = [f for f in files if f.endswith(".py")]
    py_marker_dirs = sorted(
        {
            normalize_path(os.path.dirname(f).replace("\\", "/")) or "."
            for f in files
            if os.path.basename(f) in PY_MARKERS
        }
    )
    py_projects: list[str] = []
    for directory in py_marker_dirs:
        if _dir_has_suffix(files, directory, (".py",)):
            py_projects.append(directory)
    if py_files and not py_projects:
        py_projects.append(".")
    elif py_files:
        # Directories outside every marker project still contain Python source (PY-4).
        uncovered = [
            f
            for f in py_files
            if not any(f.startswith(f"{p}/") for p in py_projects if p != ".")
        ]
        if uncovered:
            py_projects.append(".")

    # --- JS / TS ------------------------------------------------------------
    pkg_paths = [f for f in files if os.path.basename(f) == "package.json"]
    packages = [load_package_json(root, p) for p in pkg_paths]
    packages.sort(key=lambda p: p.path)
    monorepo_root: str | None = None
    root_pkg = next((p for p in packages if p.dir == "."), None)
    if root_pkg is not None and _declares_workspaces(root, root_pkg):
        monorepo_root = "."
    else:
        for pkg in packages:
            if _declares_workspaces(root, pkg):
                monorepo_root = pkg.dir
                break
    js_projects: list[str] = []
    if packages:
        js_projects = [monorepo_root or pkg.dir for pkg in packages]
        if monorepo_root is not None:
            # Nested packages stay visible as projects; their coverage is
            # inherited from the single monorepo run (JS-4).
            js_projects.extend(pkg.dir for pkg in packages)

    # --- Dart ---------------------------------------------------------------
    dart_projects = sorted(
        {
            normalize_path(os.path.dirname(f).replace("\\", "/")) or "."
            for f in files
            if os.path.basename(f) in DART_MARKERS
        }
    )
    dart_loose = [f for f in files if f.endswith(".dart")]
    if dart_loose and not dart_projects:
        dart_projects = sorted(
            {
                normalize_path(os.path.dirname(f).replace("\\", "/")) or "."
                for f in dart_loose
                if "/" not in f or f.count("/") == 0
            }
        ) or ["."]

    def add(path: str, lang: str, markers: dict[str, str] | None = None) -> None:
        for project in projects:
            if project.path == path:
                if lang not in project.langs:
                    project.langs.append(lang)
                    project.langs.sort()
                if markers:
                    project.markers.update(markers)
                return
        projects.append(
            ProjectInfo(path=path, langs=[lang], markers=dict(markers or {}))
        )

    for directory in sorted(set(py_projects)):
        found = next((m for m in PY_MARKERS if (root / directory / m).is_file()), None)
        add(directory, "python", {"python": found} if found else {})
    for directory in sorted(set(js_projects)):
        add(directory, "js", {"js": "package.json"})
    for directory in sorted(set(dart_projects)):
        add(directory, "dart", {"dart": "pubspec.yaml"})

    projects.sort(key=lambda p: (p.path != ".", p.path))

    skipped: list[dict[str, str]] = []
    project_limit_reached = False
    if len(projects) > max_projects:
        dropped = projects[max_projects:]
        projects = projects[:max_projects]
        project_limit_reached = True
        skipped.extend(
            {"project": p.path, "reason": "project limit reached (max_projects)"} for p in dropped
        )

    file_index: dict[str, list[str]] = {"python": [], "js": [], "dart": []}
    for lang, suffixes in SOURCE_SUFFIXES.items():
        file_index[lang] = [f for f in files if f.endswith(suffixes)]

    discovery = Discovery(
        projects=projects,
        packages=packages,
        monorepo_root=monorepo_root,
        root=root,
        all_files=files,
        truncated=bool(stats["truncated"]),
        project_limit_reached=project_limit_reached,
        meta={
            "skipped_secret_files": stats["skipped_secret_files"],
            "skipped_links": stats["skipped_links"],
        },
        skipped=skipped,
        file_index=file_index,
    )
    return discovery


def iter_source_files(root: Path, lang: str) -> Iterator[Path]:
    """Yield source files of one language, skipping secrets and dependency trees."""
    for rel in iter_files(root)[0]:
        if rel.endswith(SOURCE_SUFFIXES[lang]):
            yield root / rel