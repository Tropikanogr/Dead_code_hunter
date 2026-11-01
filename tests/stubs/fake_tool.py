"""Controllable stand-in for an external analyzer.

Usage: ``python fake_tool.py <config.json> [analyzer args...]``

The config describes one run: exit code, stdout, stderr and files the tool
should write (used to simulate an analyzer that mutates the repository).
``by_cwd`` selects a different run description for a project directory, which
is what a monorepo needs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    config_path = Path(sys.argv[1])
    analyzer_args = sys.argv[2:]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    cwd = Path.cwd().as_posix()
    entry = config
    for suffix, override in (config.get("by_cwd") or {}).items():
        if cwd.endswith("/" + suffix) or cwd.endswith("\\" + suffix) or cwd == suffix:
            entry = override
            break
    if "--version" in analyzer_args:
        sys.stdout.write(str(config.get("version", "9.9.9")) + "\n")
        return 0
    stdout = str(entry.get("stdout", "")).replace("{cwd}", cwd).replace("{root}", str(config_path.parent))
    sys.stdout.write(stdout)
    sys.stderr.write(str(entry.get("stderr", "")))
    for target in entry.get("touch", []):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("written by analyzer\n", encoding="utf-8")
    return int(entry.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
