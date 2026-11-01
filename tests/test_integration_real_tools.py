"""Integration tests against the analyzers actually installed on this host.

Each test skips when its tool is absent, so the suite never claims a PASS for a
tool that did not run.  Nothing is installed or downloaded here.
"""

from __future__ import annotations

import shutil

import pytest

from dead_code_hunter.models import Coverage, Recommendation
from dead_code_hunter.scanner import ScanOptions, scan

from .conftest import materialize, overrides_for
from dead_code_hunter.tools import ToolOverrides

HAS_RUFF = shutil.which("ruff") is not None
HAS_VULTURE = shutil.which("vulture") is not None
HAS_DART = shutil.which("dart") is not None


def has_local_knip(repo) -> bool:
    return any(
        (repo / "node_modules" / ".bin" / name).is_file()
        for name in ("knip", "knip.cmd", "knip.ps1")
    )


def scan_with_real_tools(repo, langs, scan_id, extra_skip=()):
    overrides = ToolOverrides()
    for tool in extra_skip:
        overrides.disabled.add(tool)
    return scan(
        ScanOptions(
            root=repo,
            langs=langs,
            overrides=overrides,
            write_reports=False,
            scan_id=scan_id,
        )
    )


@pytest.mark.skipif(not HAS_RUFF, reason="ruff is not installed on this host")
def test_real_ruff_finds_an_unused_import(tmp_path):
    repo = materialize(tmp_path, "F-PY-02")
    out = scan_with_real_tools(repo, ("python",), "real-ruff-test", extra_skip=("vulture",))
    entry = next(e for e in out.report["coverage"] if e["tool"] == "ruff")
    assert entry["coverage"] == Coverage.COMPLETE.value
    assert entry["tool_path"]
    assert entry["tool_version"]
    finding = next(f for f in out.findings if f.symbol == "json")
    assert finding.recommendation is Recommendation.CANDIDATE


@pytest.mark.skipif(not HAS_VULTURE, reason="vulture is not installed on this host")
def test_real_vulture_finds_an_unused_function(tmp_path):
    repo = materialize(tmp_path, "F-PY-01")
    out = scan_with_real_tools(repo, ("python",), "real-vulture-test", extra_skip=("ruff",))
    entry = next(e for e in out.report["coverage"] if e["tool"] == "vulture")
    assert entry["coverage"] == Coverage.COMPLETE.value
    assert entry["tool_version"]
    finding = next(f for f in out.findings if f.symbol == "calculate_internal_margin")
    assert finding.recommendation is Recommendation.CANDIDATE


@pytest.mark.skipif(not HAS_VULTURE, reason="vulture is not installed on this host")
def test_real_vulture_syntax_error_is_partial_not_success(tmp_path):
    """BUG-03 regression with the real binary: exit 1 must never look like success."""
    repo = materialize(tmp_path, "F-PY-13")
    out = scan_with_real_tools(repo, ("python",), "real-vulture-partial-test", extra_skip=("ruff",))
    entry = next(e for e in out.report["coverage"] if e["tool"] == "vulture")
    assert entry["coverage"] != Coverage.COMPLETE.value
    assert entry["coverage"] in {Coverage.PARTIAL.value, Coverage.FAILED.value}


@pytest.mark.skipif(not HAS_DART, reason="dart is not installed on this host")
def test_real_dart_analyze_reports_complete_and_flags_the_orphan(tmp_path):
    repo = materialize(tmp_path, "F-DT-01")
    out = scan_with_real_tools(repo, ("dart",), "real-dart-test")
    entry = next(e for e in out.report["coverage"] if e["tool"] == "dart analyze")
    assert entry["coverage"] == Coverage.COMPLETE.value
    finding = next(f for f in out.findings if f.file == "lib/screens/legacy_screen.dart")
    assert finding.recommendation is Recommendation.CANDIDATE


def test_real_knip_runs_only_from_a_local_installation(tmp_path):
    """knip has no host-wide installation: without node_modules it is never RUN."""
    repo = materialize(tmp_path, "F-JS-09a")
    if has_local_knip(repo):
        pytest.skip("fixture unexpectedly ships a local knip; nothing to verify here")
    out = scan_with_real_tools(repo, ("js",), "real-knip-test")
    entry = next(e for e in out.report["coverage"] if e["tool"] == "knip")
    assert entry["coverage"] != Coverage.COMPLETE.value
    assert entry["tool_path"] is None or "node_modules" in (entry["reason"] or "")


def test_absent_tools_are_reported_as_failed_not_silently_skipped(tmp_path):
    """Whichever analyzers are missing here must appear as FAILED, never COMPLETE."""
    repo = materialize(tmp_path, "F-PY-01")
    out = scan(
        ScanOptions(
            root=repo,
            langs=("python",),
            overrides=overrides_for(tmp_path, "F-PY-01", skip=("ruff", "vulture")),
            write_reports=False,
            scan_id="absent-tools-test",
        )
    )
    statuses = {e["tool"]: e["coverage"] for e in out.report["coverage"] if e["lang"] == "python"}
    assert statuses == {"ruff": "FAILED", "vulture": "FAILED"}
    assert out.report["tools"]
    assert any(tool["missing_reason"] for tool in out.report["tools"])


def test_tool_availability_matrix_is_reported_honestly(tmp_path):
    """Every language in scope must have a coverage row, RUN or explicitly missing."""
    repo = materialize(tmp_path, "F-PY-01")
    out = scan_with_real_tools(repo, ("python",), "availability-matrix-test")
    tools = {tool["name"]: tool for tool in out.report["tools"]}
    assert "ruff" in tools
    for entry in out.report["coverage"]:
        if entry["lang"] != "python":
            continue
        if entry["coverage"] == Coverage.FAILED.value:
            assert entry["reason"], "a failed analyzer must always carry a reason"
        else:
            assert entry["tool_version"], (
                f"{entry['tool']} reported success without recording its version"
            )
