"""Project discovery: Python without markers, monorepos, limits, secrets."""

from __future__ import annotations

from pathlib import Path

from dead_code_hunter.discovery import discover, iter_files
from dead_code_hunter.scanner import ScanOptions, scan

from .conftest import overrides_for


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_py_4_directory_with_python_but_no_markers_is_scanned(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "scripts" / "tool.py", "def unused_helper():\n    return 1\n")
    discovery = discover(repo)
    assert any("python" in project.langs for project in discovery.projects)
    assert discovery.owner("scripts/tool.py") in {".", "scripts"}

    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-PY-01"),
            write_reports=False,
            scan_id="discovery-test",
        )
    )
    assert any(entry["lang"] == "python" for entry in out.report["coverage"])


def test_js_4_workspace_root_is_one_run(tmp_path):
    repo = tmp_path / "repo"
    write(
        repo / "package.json",
        '{"name": "mono", "private": true, "workspaces": ["packages/*"]}\n',
    )
    write(repo / "packages" / "a" / "package.json", '{"name": "a"}\n')
    write(repo / "packages" / "a" / "src" / "a.ts", "export const a = 1;\n")
    write(repo / "packages" / "b" / "package.json", '{"name": "b"}\n')
    write(repo / "packages" / "b" / "src" / "b.ts", "export const b = 1;\n")

    discovery = discover(repo)
    assert discovery.monorepo_root == "."

    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-JS-01"),
            write_reports=False,
            scan_id="monorepo-test",
        )
    )
    knip_entries = [entry for entry in out.report["coverage"] if entry["tool"] == "knip"]
    executed = [entry for entry in knip_entries if not entry["inherited"]]
    assert len(executed) == 1
    assert executed[0]["project"] == "."
    inherited = {entry["project"]: entry for entry in knip_entries if entry["inherited"]}
    assert {"packages/a", "packages/b"} <= set(inherited)
    assert "covered by the run at '.'" in inherited["packages/a"]["reason"]
    projects = {project["path"] for project in out.report["projects"]}
    assert {"packages/a", "packages/b"} <= projects


def test_js_4_pnpm_workspace_file_is_detected(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "package.json", '{"name": "mono", "private": true}\n')
    write(repo / "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    write(repo / "packages" / "a" / "package.json", '{"name": "a"}\n')
    assert discover(repo).monorepo_root == "."


def test_uncovered_package_json_makes_the_scan_partial(tmp_path):
    repo = tmp_path / "repo"
    write(
        repo / "web" / "package.json",
        '{"name": "web", "private": true, "workspaces": ["packages/*"]}\n',
    )
    write(repo / "web" / "packages" / "a" / "package.json", '{"name": "a"}\n')
    write(repo / "server" / "package.json", '{"name": "server", "private": true}\n')
    write(repo / "server" / "src" / "index.ts", "export const x = 1;\n")

    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-JS-01"),
            write_reports=False,
            scan_id="uncovered-test",
        )
    )
    reasons = " ".join(
        str(entry["reason"]) for entry in out.report["coverage"] if entry["tool"] == "knip"
    )
    assert "server/package.json" in reasons
    assert any(
        entry["tool"] == "knip" and entry["coverage"] != "COMPLETE"
        for entry in out.report["coverage"]
    )


def test_project_limit_is_declared_not_silent(tmp_path):
    repo = tmp_path / "repo"
    for index in range(3):
        write(repo / f"svc{index}" / "package.json", '{"name": "svc"}\n')
        write(repo / f"svc{index}" / "src" / "index.ts", "export const x = 1;\n")
    write(repo / ".dead-code" / "config.toml", "[scan]\nmax_projects = 1\n")

    out = scan(
        ScanOptions(
            root=repo,
            overrides=overrides_for(tmp_path, "F-JS-01"),
            write_reports=False,
            scan_id="limit-test",
        )
    )
    assert out.report["discovery"]["project_limit_reached"] is True
    assert out.report["discovery"]["skipped_projects"]


def test_secret_files_and_dependency_trees_are_not_traversed(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "app.py", "print(1)\n")
    write(repo / ".env", "SECRET=1\n")
    write(repo / "node_modules" / "pkg" / "index.js", "module.exports = 1;\n")
    write(repo / ".venv" / "lib" / "pkg.py", "x = 1\n")

    files, stats = iter_files(repo)
    assert ".env" not in files
    assert not any(path.startswith("node_modules/") for path in files)
    assert not any(path.startswith(".venv/") for path in files)
    assert stats["skipped_secret_files"] >= 1


def test_symlinks_are_not_followed(tmp_path):
    repo = tmp_path / "repo"
    write(repo / "app.py", "print(1)\n")
    outside = tmp_path / "outside.py"
    outside.write_text("print(2)\n", encoding="utf-8")
    link = repo / "linked.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink creation not permitted on this host")
    files, stats = iter_files(repo)
    assert "linked.py" not in files
    assert stats["skipped_links"] >= 1
