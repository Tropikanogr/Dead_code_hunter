"""Safety primitives: secret exclusion, environment scrubbing, no downloads.

Nothing here installs, downloads or executes repository content by itself.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

#: Files that must never be opened, neither for scanning nor for indexing (spec X-1/TM-1).
SECRET_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*.json",
    "secrets.*",
    "*.secret",
    ".npmrc",
    ".pypirc",
    ".netrc",
)

#: Directories that are never traversed.  Dependency and VCS trees are huge and
#: their contents are not the user's source code.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
        ".next", ".nuxt", ".svelte-kit", "dist", "build", "out", ".dart_tool",
        ".idea", ".vscode-test", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".tox", ".nox", "coverage", ".gradle", "Pods", "__pycache__", ".turbo",
        ".output", ".git-worktrees", ".yarn", ".pnpm-store", "build_runner",
    }
)

_ENV_SECRET_PATTERNS = (
    "*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "*PASSWD*", "*_URL*",
    "AWS_*", "IBKR*", "*CREDENTIAL*", "*_DSN*",
)

_SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "USERNAME", "USER", "LANG", "LC_ALL", "TZ",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS",
    "PROGRAMFILES", "PROGRAMDATA", "PUBLIC",
    "HOMEDRIVE", "HOMEPATH", "NODE_PATH", "DART_SDK", "FLUTTER_ROOT",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE",
}


def is_secret_path(name: str) -> bool:
    """True when a file or directory name matches the never-read list."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pat.lower()) for pat in SECRET_FILE_PATTERNS)


def scrub_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an allowlisted environment with analyzer-friendly defaults.

    The scrubbed env keeps the OS essentials (PATH, TEMP, ...) and drops
    anything that looks like a credential or a database URL.
    """
    source = dict(os.environ if base is None else base)
    scrubbed: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if any(fnmatch.fnmatch(upper, pat.upper()) for pat in _ENV_SECRET_PATTERNS):
            continue
        if key in _SAFE_ENV_KEYS or upper in _SAFE_ENV_KEYS:
            scrubbed[key] = value
    scrubbed.update(
        {
            "CI": "1",
            "NO_COLOR": "1",
            "npm_config_offline": "true",
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return scrubbed


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` stays inside ``root`` after symlink resolution."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(base)
    except ValueError:
        return False
    return True
