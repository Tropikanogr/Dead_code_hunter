"""Integration tests against the analyzers actually installed on this host.

Each test skips when its tool is absent, so the suite never claims a PASS for a
tool that did not run.
"""

from __future__ import annotations

import shutil

import pytest

from dead_code_hunter.models import Coverage, Recommendation
from dead_code_hunter.scanner import ScanOptions, scan

from .conftest import materialize, overrides_for
from dead_code_hunter.tools import ToolOverrides

HAS_RUFF = shutil.which("ruff") is not None
HAS_DART = shutil.which("dart") is not None


@pytest.mark.skipif(not HAS_RUFF, reason="ruff is not installed on this host")
def test_real_ruff_finds_an_unused_import(tmp_path):
    repo = materialize(tmp_path, "F-PY-02")
    overrides = ToolOverrides()
    overrides.disabled.add("vulture")  # isolate ruff; the other tool is covered elsewhere
    out = scan(
        ScanOptions(
            root=repo,
            langs=("python",),
            overrides=overrides,
            write_reports=False,
            scan_id="real-ruff-test",
        )
    )
    entry = next(e for e in out.report["coverage"] if e["tool"] == "ruff")
    assert entry["coverage"] == Coverage.COMPLETE.value
    assert entry["tool_path"]
    assert entry["tool_version"]
    finding = next(f for f in out.findings if f.symbol == "json")
    assert finding.recommendation is Recommendation.CANDIDATE


@pytest.mark.skipif(not HAS_DART, reason="dart is not installed on this host")
def test_real_dart_analyze_reports_complete_and_flags_the_orphan(tmp_path):
    repo = materialize(tmp_path, "F-DT-01")
    out = scan(
        ScanOptions(
            root=repo,
            langs=("dart",),
            overrides=ToolOverrides(),
            write_reports=False,
            scan_id="real-dart-test",
        )
    )
    entry = next(e for e in out.report["coverage"] if e["tool"] == "dart analyze")
    assert entry["coverage"] == Coverage.COMPLETE.value
    finding = next(f for f in out.findings if f.file == "lib/screens/legacy_screen.dart")
    assert finding.recommendation is Recommendation.CANDIDATE


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
