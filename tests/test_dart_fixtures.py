"""Dart fixtures F-DT-01 … F-DT-07."""

from __future__ import annotations

from dead_code_hunter.models import Confidence, Contract, Coverage, Recommendation, Rule

from .conftest import by_file, coverage_of, one, run_fixture
from .fixtures_repo import ACTIVE_SYMBOLS


def test_f_dt_01_orphan_screen_is_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-DT-01")
    finding = by_file(out, "lib/screens/legacy_screen.dart")
    assert finding.kind == "file"
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8


def test_f_dt_02_conditional_import_branch_is_not_orphan(tmp_path):
    out = run_fixture(tmp_path, "F-DT-02")
    for path in ("lib/platform/io_loader.dart", "lib/platform/web_loader.dart"):
        assert not [f for f in out.findings if f.file == path], f"{path} must not be orphaned"


def test_f_dt_03_entry_point_pragma_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-DT-03")
    finding = by_file(out, "lib/background/sync_handler.dart")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED


def test_f_dt_04_cross_package_lib_src_is_not_orphan(tmp_path):
    out = run_fixture(tmp_path, "F-DT-04")
    assert not [f for f in out.findings if f.file == "packages/core/lib/src/models.dart"]
    projects = {entry["project"] for entry in out.report["coverage"]}
    assert {"packages/app", "packages/core"} <= projects


def test_f_dt_05_test_only_file_requires_review(tmp_path):
    out = run_fixture(tmp_path, "F-DT-05")
    finding = by_file(out, "lib/support/fake_clock.dart")
    assert finding.confidence is Confidence.LOW
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R7
    assert "reachable-only-from-tests" in finding.signals


def test_f_dt_06_route_map_screen_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-DT-06")
    finding = one(out, "OrdersScreen")
    assert finding.recommendation is Recommendation.RETAIN
    assert finding.rule is Rule.R3


def test_f_dt_07_analyzer_failure_is_failed_never_ok(tmp_path):
    out = run_fixture(tmp_path, "F-DT-07")
    entry = coverage_of(out, "dart analyze")
    assert entry["coverage"] == Coverage.FAILED.value
    assert "exit" in entry["reason"] or "exited" in entry["reason"]
    # The import graph cannot claim complete coverage when the analyzer failed.
    graph = coverage_of(out, "dart-import-graph")
    assert graph["coverage"] != Coverage.COMPLETE.value
    finding = by_file(out, "lib/broken.dart")
    assert finding.recommendation is not Recommendation.CANDIDATE


def test_no_active_dart_file_is_a_candidate(tmp_path):
    for name, symbols in ACTIVE_SYMBOLS.items():
        if not name.startswith("F-DT"):
            continue
        out = run_fixture(tmp_path / name, name)
        for symbol in symbols:
            for finding in [f for f in out.findings if f.symbol == symbol]:
                assert finding.recommendation is not Recommendation.CANDIDATE, (
                    f"{name}: active symbol {symbol} reached CANDIDATE"
                )
