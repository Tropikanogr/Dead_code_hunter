"""Git status integrity (spec X-2 / BUG-10)."""

from __future__ import annotations

import subprocess

from dead_code_hunter import gitstate
from dead_code_hunter.scanner import ScanOptions, scan

from .conftest import overrides_for


def write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "pyproject.toml", "[project]\nname='dch-git'\nversion='0.1.0'\n")
    write(repo / "app" / "__init__.py", "")
    write(repo / "app" / "core.py", "def helper():\n    return 1\n")
    write(repo / ".dead-code" / "config.toml", "[scan]\ntimeout_seconds = 60\n")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "tests")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_scan_leaves_git_status_untouched(tmp_path):
    repo = make_repo(tmp_path)
    before = gitstate.capture(repo)
    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-PY-01"),
            write_reports=False,
            scan_id="git-test",
        )
    )
    after = gitstate.capture(repo)
    assert before.porcelain == after.porcelain
    assert out.report["git_integrity"]["changed"] is False
    assert out.report["git_integrity"]["verifiable"] is True
    assert out.exit_code == 0
    assert out.report["revision"]["head"] == after.head


def test_preexisting_untracked_config_is_not_a_change(tmp_path):
    repo = make_repo(tmp_path)
    write(repo / ".dead-code" / "ignore", "# nothing ignored\n")
    before = gitstate.capture(repo)
    assert before.dirty is True  # the ignore file is untracked
    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-PY-01"),
            write_reports=False,
            scan_id="git-untracked-test",
        )
    )
    assert out.report["git_integrity"]["changed"] is False
    assert out.exit_code == 0


def test_non_git_directory_is_reported_as_unverifiable(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "app.py", "print(1)\n")
    state = gitstate.capture(repo)
    assert state.status in {"not-a-repo", "unavailable"}
    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-PY-01"),
            write_reports=False,
            scan_id="git-none-test",
        )
    )
    assert out.report["git_integrity"]["verifiable"] is False
    assert out.report["git_integrity"]["changed"] is None
