"""CLI behaviour: formats, exit codes and report locations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dead_code_hunter import cli

from .fixtures_repo import pyproject


def write_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(pyproject("dch-cli"), encoding="utf-8")
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "core.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return repo


def test_scan_prints_json(tmp_path, capsys):
    repo = write_repo(tmp_path)
    code = cli.main(["scan", "--repo", str(repo), "--no-write", "--format", "json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["repository"]["path"].endswith("repo")
    assert code == 0


def test_scan_summary_is_human_readable(tmp_path, capsys):
    repo = write_repo(tmp_path)
    code = cli.main(["scan", "--repo", str(repo), "--no-write"])
    captured = capsys.readouterr()
    assert "Dead Code Hunter" in captured.out
    assert "coverage:" in captured.out
    assert code == 0


def test_reports_go_to_the_reports_directory(tmp_path, capsys):
    repo = write_repo(tmp_path)
    reports = tmp_path / "reports"
    code = cli.main(
        [
            "scan",
            "--repo",
            str(repo),
            "--reports-dir",
            str(reports),
            "--print-report-path",
        ]
    )
    captured = capsys.readouterr()
    paths = [line for line in captured.out.splitlines() if line.strip()]
    assert paths
    for path in paths:
        assert not Path(path).is_relative_to(repo)
    assert code in (0, 2)


def test_strict_mode_fails_when_a_tool_is_missing(tmp_path):
    repo = write_repo(tmp_path)
    # Simulate an absent vulture instead of assuming anything about this host.
    from dead_code_hunter import cli
    import dead_code_hunter.scanner as scanner_module
    from dead_code_hunter.tools import ToolOverrides

    overrides = ToolOverrides()
    overrides.disabled.add("vulture")
    original = scanner_module.ScanOptions

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        cli,
        "ScanOptions",
        lambda **kwargs: original(**{**kwargs, "overrides": overrides, "write_reports": False}),
        raising=True,
    )
    try:
        code = cli.main(["scan", "--repo", str(repo), "--no-write", "--strict"])
    finally:
        monkey.undo()
    assert code == 1


def test_missing_repository_is_a_clean_error(tmp_path, capsys):
    code = cli.main(["scan", "--repo", str(tmp_path / "nope"), "--no-write"])
    captured = capsys.readouterr()
    assert code == 1
    assert "error" in captured.err.lower()


def test_help_without_command():
    assert cli.main([]) == 1


def test_language_filter_limits_adapters(tmp_path, capsys):
    repo = write_repo(tmp_path)
    (repo / "package.json").write_text('{"name": "x", "private": true}\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
    code = cli.main(["scan", "--repo", str(repo), "--no-write", "--format", "json", "--lang", "python"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["scope"]["langs"] == ["python"]
    assert all(entry["lang"] == "python" for entry in payload["coverage"])
    assert code == 0
