"""Locating and running local analyzers.

Rules enforced here:
* allowlist of executables only (spec TM-3),
* ``shell=False`` always, with ``cmd.exe`` used only to launch ``.cmd`` shims,
* output and time limits,
* no network, no installation, no PATH fallback for tools that must be local.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .safety import scrub_env

OUTPUT_LIMIT = 4 * 1024 * 1024
ALLOWED_EXECUTABLES = frozenset({"ruff", "vulture", "knip", "dart", "git"})


@dataclass(frozen=True)
class Command:
    """An executable plus fixed leading arguments (e.g. ``cmd.exe /c shim``).

    ``tool`` names the allowlisted analyzer this command implements.  Real
    tool lookups leave it empty (the executable name is then checked); test
    seams must declare the tool they emulate.
    """

    executable: str
    prefix_args: tuple[str, ...] = ()
    tool: str | None = None

    def argv(self, args: list[str] | tuple[str, ...]) -> list[str]:
        return [self.executable, *self.prefix_args, *args]

    def describe(self) -> str:
        return " ".join([self.executable, *self.prefix_args])

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "prefix_args": list(self.prefix_args),
            "tool": self.tool,
        }


@dataclass
class RunResult:
    status: str  # executed | timeout | error
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "executed"


def wrap_executable(path: str) -> Command:
    """Build a shell-free command for ``path``.

    ``.cmd``/``.bat`` shims (npm's ``node_modules/.bin`` layout on Windows)
    cannot be started by ``CreateProcess`` directly, so they are launched
    through ``cmd.exe /c`` with a fixed argv.  No shell string is ever built.
    """
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        shell = os.environ.get("COMSPEC") or "cmd.exe"
        return Command(shell, ("/c", path))
    return Command(path)


def _find_project_local(names: list[str], start: Path, root: Path) -> Path | None:
    """Search ``start`` and its ancestors up to ``root`` (inclusive)."""
    folder = start
    while True:
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
        if folder == root or folder.parent == folder:
            return None
        try:
            folder.relative_to(root)
        except ValueError:
            return None
        folder = folder.parent


def find_python_tool(name: str, project_root: Path, repo_root: Path) -> Command | None:
    """Project ``.venv`` first, then PATH (spec PY-3)."""
    if name not in ALLOWED_EXECUTABLES:
        raise ValueError(f"executable not allowed: {name}")
    windows = os.name == "nt"
    local_names = (
        [f".venv/Scripts/{name}.exe", f".venv/Scripts/{name}"]
        if windows
        else [f".venv/bin/{name}"]
    )
    found = _find_project_local(local_names, project_root, repo_root)
    if found is not None:
        return wrap_executable(str(found))
    on_path = shutil.which(name)
    return wrap_executable(on_path) if on_path else None


def find_local_knip(project_root: Path, repo_root: Path) -> Command | None:
    """Only ``node_modules/.bin/knip`` counts.  A global knip never does (JS-1)."""
    names = ["node_modules/.bin/knip", "node_modules/.bin/knip.cmd", "node_modules/.bin/knip.ps1"]
    found = _find_project_local(names, project_root, repo_root)
    return wrap_executable(str(found)) if found else None


def find_dart() -> Command | None:
    """The Dart SDK is a host tool; no repository-provided executable is used."""
    on_path = shutil.which("dart")
    return wrap_executable(on_path) if on_path else None


VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.\-]+)?)")


def run(
    command: Command,
    args: list[str],
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Run an allowlisted command with no shell, scrubbed env and hard limits."""
    executable_name = Path(command.executable).name.lower()
    stem = (command.tool or executable_name.split(".")[0]).lower()
    if stem not in ALLOWED_EXECUTABLES and executable_name not in {"cmd.exe", "cmd"}:
        raise ValueError(f"executable not allowed: {command.executable}")
    argv = command.argv(args)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=dict(scrub_env() if env is None else env),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return RunResult(status="timeout", error=f"timeout after {timeout}s")
    except (OSError, ValueError) as exc:
        return RunResult(status="error", error=str(exc))
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    truncated = len(stdout) > OUTPUT_LIMIT or len(stderr) > OUTPUT_LIMIT
    return RunResult(
        status="executed",
        exit_code=completed.returncode,
        stdout=stdout[:OUTPUT_LIMIT],
        stderr=stderr[:OUTPUT_LIMIT],
        truncated=truncated,
    )


def tool_version(command: Command, args: list[str], cwd: Path, timeout: int = 30) -> str | None:
    result = run(command, args, cwd, timeout=timeout)
    if not result.ok:
        return None
    text = f"{result.stdout}\n{result.stderr}".strip()
    match = VERSION_RE.search(text)
    if match:
        return match.group(1)
    first = text.splitlines()[0].strip() if text else ""
    return first or None


@dataclass
class ToolOverrides:
    """Test seam: replace tool resolution and arguments with controlled values."""

    commands: dict[str, Command] = field(default_factory=dict)
    arguments: dict[str, list[str]] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)

    def get(self, name: str) -> Command | None:
        return self.commands.get(name)

    def args_for(self, name: str, default: list[str]) -> list[str]:
        return list(self.arguments.get(name, default))

    def resolve(self, name: str, finder) -> Command | None:
        """Explicit override → simulated absence → normal lookup."""
        if name in self.disabled:
            return None
        override = self.commands.get(name)
        return override if override is not None else finder()
