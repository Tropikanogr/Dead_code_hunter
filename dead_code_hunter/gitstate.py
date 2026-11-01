"""Git status / revision capture used to prove the scanner did not touch the repo."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GitState:
    status: str  # ok | not-a-repo | unavailable
    porcelain: str = ""
    head: str | None = None
    detail: str | None = None

    @property
    def dirty(self) -> bool | None:
        if self.status != "ok":
            return None
        return bool(self.porcelain.strip())

    def dirty_files(self) -> list[str]:
        files = []
        for line in self.porcelain.splitlines():
            if not line.strip():
                continue
            # porcelain v1: "XY path" (renames add " -> orig")
            path = line[3:] if len(line) > 3 else line.strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip().strip('"').replace("\\", "/"))
        return files

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "porcelain": self.porcelain,
            "head": self.head,
            "dirty": self.dirty,
            "dirty_files": self.dirty_files(),
            "detail": self.detail,
        }


def _git(args: list[str], root: Path, timeout: int = 20) -> tuple[int | None, str, str]:
    git = shutil.which("git")
    if not git:
        return None, "", "git executable not found"
    try:
        completed = subprocess.run(
            [git, *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def capture(root: Path) -> GitState:
    code, out, err = _git(["status", "--porcelain=v1", "--untracked-files=normal"], root)
    if code is None:
        return GitState(status="unavailable", detail=err.strip() or None)
    if code != 0:
        return GitState(status="not-a-repo", detail=(err or out).strip()[:400] or None)
    head_code, head_out, _ = _git(["rev-parse", "HEAD"], root)
    return GitState(
        status="ok",
        porcelain=out,
        head=head_out.strip() if head_code == 0 and head_out.strip() else None,
    )


@dataclass
class GitIntegrity:
    before: GitState
    after: GitState
    changed: bool | None = None
    changed_files: list[str] = field(default_factory=list)

    @property
    def verifiable(self) -> bool:
        return self.before.status == "ok" and self.after.status == "ok"

    def evaluate(self) -> "GitIntegrity":
        if self.verifiable:
            self.changed = self.before.porcelain != self.after.porcelain
            if self.changed:
                before_files = set(self.before.dirty_files())
                after_files = set(self.after.dirty_files())
                added = sorted(after_files - before_files)
                self.changed_files = added or sorted(after_files)
        else:
            self.changed = None
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "changed": self.changed,
            "changed_files": self.changed_files,
            "verifiable": self.verifiable,
            # Honest scope limitation: `git status --porcelain` ignores
            # .gitignore'd paths, so a write into an ignored file is invisible
            # to this check (it is still reported as unverifiable coverage).
            "ignored_files_tracked": False,
            "ignored_files_note": (
                "git status does not report files matched by .gitignore "
                "(.dart_tool/, build outputs, caches); writes into those paths are "
                "outside the integrity check."
            ),
        }
