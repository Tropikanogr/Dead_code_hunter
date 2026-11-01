"""Known entrypoints that do not live in a package manifest.

Spec DP-5/JS-5: files started from a Dockerfile, a Procfile, a shell script or a
pm2 configuration must be protected and named in the report, whether or not the
dependency analyzer knows about them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..models import normalize_path
from ..safety import is_secret_path

SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".ps1", ".cmd", ".bat")
CONFIG_NAMES = {
    "ecosystem.config.js", "ecosystem.config.cjs", "ecosystem.config.mjs",
    "ecosystem.config.json", "pm2.config.js", "pm2.config.json", "processes.json",
}
_RUNNER = re.compile(
    r"(?:^|[\s\"'=(])"
    r"(?:node|nodejs|tsx|ts-node|nodemon|bun|deno\s+run|dart\s+run|dart|python|python3|py)"
    r"\s+(?P<path>[^\s\"';&|]+)"
)
_BARE = re.compile(r"(?:^|[\s\"'=(])(?P<path>[A-Za-z0-9_./\\-]+\.(?:js|mjs|cjs|ts|dart))(?=[\s\"');&|]|$)")
_SCRIPT_FIELD = re.compile(r"\"?script\"?\s*[:=]\s*[\"'](?P<path>[^\"']+)[\"']")
_CMD = re.compile(r"^\s*(?:CMD|ENTRYPOINT)\s+(?P<rest>.+)$", re.IGNORECASE)
_PROCFILE = re.compile(r"^[A-Za-z0-9_-]+\s*:\s*(?P<cmd>.+)$")


@dataclass(frozen=True)
class Entrypoint:
    path: str  # repo-relative POSIX path of the entry file ("" when unresolved)
    source: str  # which file declared it
    command: str
    kind: str  # dockerfile | procfile | script | pm2


def _clean(token: str) -> str:
    token = token.strip().strip("\"'`")
    token = token.split("#", 1)[0]
    for prefix in ("./", ".\\"):
        while token.startswith(prefix):
            token = token[len(prefix):]
    return token


def _resolve(base_dir: str, token: str, known_files: set[str]) -> str | None:
    token = _clean(token)
    if not token or "://" in token or token.startswith("-"):
        return None
    token = token.replace("\\", "/")
    candidates = []
    if base_dir not in ("", "."):
        candidates.append(normalize_path(f"{base_dir}/{token}"))
    candidates.append(normalize_path(token))
    for candidate in candidates:
        if candidate in known_files:
            return candidate
        stripped = candidate.lstrip("/")
        if stripped in known_files:
            return stripped
    return None


def _extract(command: str, base_dir: str, known_files: set[str], source: str, kind: str) -> list[Entrypoint]:
    found: list[Entrypoint] = []
    seen: set[str] = set()
    for match in _RUNNER.finditer(command):
        resolved = _resolve(base_dir, match.group("path"), known_files)
        if resolved and resolved not in seen:
            seen.add(resolved)
            found.append(Entrypoint(path=resolved, source=source, command=command.strip(), kind=kind))
    for match in _BARE.finditer(command):
        resolved = _resolve(base_dir, match.group("path"), known_files)
        if resolved and resolved not in seen:
            seen.add(resolved)
            found.append(Entrypoint(path=resolved, source=source, command=command.strip(), kind=kind))
    return found


def discover_entrypoints(root: Path, files: list[str]) -> tuple[list[Entrypoint], dict[str, str]]:
    """Return known external entrypoints and unreadable/unresolved declarations."""
    known = set(files)
    entrypoints: list[Entrypoint] = []
    problems: dict[str, str] = {}
    for rel in files:
        name = Path(rel).name
        base_dir = normalize_path(str(Path(rel).parent).replace("\\", "/"))
        if base_dir == "":
            base_dir = "."
        lowered = name.lower()
        if lowered == "dockerfile" or lowered.startswith("dockerfile.") or lowered.endswith(".dockerfile"):
            text = _read(root, rel, problems)
            if text is None:
                continue
            for line in _join_continuations(text):
                match = _CMD.match(line)
                if match:
                    entrypoints.extend(
                        _extract(match.group("rest"), base_dir, known, rel, "dockerfile")
                    )
        elif lowered in {"procfile", "procfile.dev"} or lowered.startswith("procfile."):
            text = _read(root, rel, problems)
            if text is None:
                continue
            for line in text.splitlines():
                match = _PROCFILE.match(line)
                if match:
                    entrypoints.extend(
                        _extract(match.group("cmd"), base_dir, known, rel, "procfile")
                    )
        elif lowered.endswith(SCRIPT_SUFFIXES):
            text = _read(root, rel, problems)
            if text is None:
                continue
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                entrypoints.extend(_extract(line, base_dir, known, rel, "script"))
        elif lowered in CONFIG_NAMES:
            text = _read(root, rel, problems)
            if text is None:
                continue
            if lowered.endswith(".json"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:
                    problems[rel] = f"invalid pm2 config json: {exc}"
                    continue
                apps = data.get("apps") if isinstance(data, dict) else None
                if isinstance(apps, list):
                    for app in apps:
                        if isinstance(app, dict) and isinstance(app.get("script"), str):
                            entrypoints.extend(
                                _extract(app["script"], base_dir, known, rel, "pm2")
                            )
            else:
                for match in _SCRIPT_FIELD.finditer(text):
                    entrypoints.extend(
                        _extract(match.group("path"), base_dir, known, rel, "pm2")
                    )
    unique: dict[tuple[str, str], Entrypoint] = {}
    for entry in entrypoints:
        unique.setdefault((entry.path, entry.source), entry)
    return sorted(unique.values(), key=lambda e: (e.path, e.source)), problems


def _read(root: Path, rel: str, problems: dict[str, str]) -> str | None:
    if is_secret_path(Path(rel).name):
        return None
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        problems[rel] = str(exc)
        return None


def _join_continuations(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buffer += raw.rstrip()[:-1] + " "
            continue
        lines.append(buffer + raw)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines
