"""JS/TS fixtures F-JS-01 … F-JS-12."""

from __future__ import annotations

from dead_code_hunter.models import Contract, Coverage, Recommendation, Rule

from .conftest import by_file, coverage_of, one, run_fixture
from .fixtures_repo import ACTIVE_SYMBOLS


def test_f_js_01_unused_export_is_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-JS-01")
    finding = one(out, "calculateUnusedTotal")
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8
    assert finding.category.value == "dead_code"


def test_f_js_02_orphan_file_is_candidate(tmp_path):
    out = run_fixture(tmp_path, "F-JS-02")
    finding = by_file(out, "src/legacy/orphan.ts")
    assert finding.kind == "file"
    assert finding.recommendation is Recommendation.CANDIDATE


def test_f_js_03_barrel_reexport_consumed_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-JS-03")
    finding = one(out, "parseOrder")
    assert finding.recommendation is Recommendation.RETAIN
    assert finding.rule is Rule.R3


def test_f_js_04_react_lazy_route_is_retained(tmp_path):
    out = run_fixture(tmp_path, "F-JS-04")
    finding = by_file(out, "src/routes/Dashboard.tsx")
    assert finding.recommendation is Recommendation.RETAIN
    assert any("dynamic import" in reason for reason in finding.reference_reasons)


def test_f_js_05_express_route_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-JS-05")
    finding = one(out, "default")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED


def test_f_js_06_socket_event_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-JS-06")
    finding = one(out, "registerRealtime")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED


def test_f_js_07_unlisted_dependency_is_info_not_dead_code(tmp_path):
    out = run_fixture(tmp_path, "F-JS-07")
    finding = one(out, "left-pad")
    assert finding.category.value == "dependency_issue"
    assert finding.recommendation is Recommendation.INFO
    assert not [f for f in out.findings if f.category.value == "dead_code"]


def test_f_js_08_monorepo_without_workspaces_covers_both_packages(tmp_path):
    out = run_fixture(tmp_path, "F-JS-08")
    projects = {entry["project"] for entry in out.report["coverage"] if entry["tool"] == "knip"}
    assert {"server", "client"} <= projects
    for project in ("server", "client"):
        entry = coverage_of(out, "knip", project)
        assert entry["coverage"] == Coverage.COMPLETE.value
    # A cross-package import is a real reference, not an orphan.
    finding = one(out, "serverReady")
    assert finding.recommendation is Recommendation.RETAIN


def test_f_js_09a_missing_node_modules_is_partial(tmp_path):
    out = run_fixture(tmp_path, "F-JS-09a")
    entry = coverage_of(out, "knip")
    assert entry["coverage"] == Coverage.PARTIAL.value
    assert "node_modules" in entry["reason"]


def test_f_js_10_missing_knip_is_failed_with_install_hint(tmp_path):
    out = run_fixture(tmp_path, "F-JS-10")
    entry = coverage_of(out, "knip")
    assert entry["coverage"] == Coverage.FAILED.value
    assert "npm i -D knip" in entry["reason"]


def test_f_js_11_worker_started_from_shell_script_is_protected(tmp_path):
    out = run_fixture(tmp_path, "F-JS-11")
    finding = by_file(out, "src/workers/mailer.js")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED
    paths = {entry["path"] for entry in out.report["entrypoints"]}
    assert "src/workers/mailer.js" in paths
    coverage = coverage_of(out, "knip")
    assert coverage["coverage"] == Coverage.PARTIAL.value
    assert "src/workers/mailer.js" in coverage["reason"]


def test_f_js_12_entry_export_is_protected_despite_include_entry_exports(tmp_path):
    out = run_fixture(tmp_path, "F-JS-12")
    finding = one(out, "calculateTotal")
    assert finding.contract is Contract.PUBLIC
    assert finding.recommendation is Recommendation.PROTECTED
    assert (out.root / "knip.json").is_file(), "fixture must keep the includeEntryExports config"


def test_no_active_js_symbol_is_a_candidate(tmp_path):
    for name, symbols in ACTIVE_SYMBOLS.items():
        if not name.startswith("F-JS"):
            continue
        out = run_fixture(tmp_path / name, name)
        for symbol in symbols:
            for finding in [f for f in out.findings if f.symbol == symbol]:
                assert finding.recommendation is not Recommendation.CANDIDATE, (
                    f"{name}: active symbol {symbol} reached CANDIDATE"
                )


def test_no_analyzer_command_downloads_or_installs_anything(tmp_path):
    for name in ("F-JS-01", "F-JS-08", "F-JS-11"):
        out = run_fixture(tmp_path / name, name)
        for entry in out.report["coverage"]:
            command = entry.get("command") or []
            flat = " ".join(str(part) for part in command).lower()
            assert "npx" not in flat
            assert "--yes" not in flat
            assert " install" not in flat
