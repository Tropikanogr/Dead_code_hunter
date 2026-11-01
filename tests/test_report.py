"""Report schema, location guarantees and determinism (spec M1 PASS criteria)."""

from __future__ import annotations

import json

import pytest

from dead_code_hunter.report import default_reports_base, report_dir

from .conftest import run_fixture

FINDING_FIELDS = {
    "id",
    "scan_id",
    "lang",
    "project",
    "tool",
    "kind",
    "category",
    "file",
    "line",
    "end_line",
    "symbol",
    "confidence",
    "confidence_reasons",
    "contract_sensitivity",
    "contract_reasons",
    "impact",
    "impact_reasons",
    "coverage",
    "coverage_reasons",
    "reference",
    "reference_reasons",
    "signals",
    "recommendation",
    "rule",
    "fingerprint",
    "external_consumers_checked",
    "evidence",
}

REPORT_FIELDS = {
    "scan_id",
    "tool_version",
    "generated_at",
    "repository",
    "revision",
    "scope",
    "projects",
    "coverage",
    "isolation",
    "ignored",
    "summary",
    "findings",
    "entrypoints",
    "tools",
    "config",
    "git_integrity",
}


def test_report_has_every_documented_field(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01")
    report = out.report
    assert REPORT_FIELDS <= set(report)
    assert set(report["revision"]) == {"head", "dirty", "dirty_files"}
    assert {"paths", "langs"} <= set(report["scope"])
    assert {"requested", "reported", "unmatched", "outside_root"} <= set(report["scope"])
    assert set(report["isolation"]) >= {
        "os_sandbox",
        "network_blocked",
        "snapshot",
        "secrets_excluded",
        "env_scrubbed",
    }
    assert report["findings"]
    for finding in report["findings"]:
        assert FINDING_FIELDS <= set(finding), FINDING_FIELDS - set(finding)
        assert finding["external_consumers_checked"] == {"supported": False, "repos": []}


def test_isolation_is_honest_about_m1_limits(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01")
    isolation = out.report["isolation"]
    assert isolation["os_sandbox"] is False
    assert isolation["network_blocked"] is False
    assert isolation["snapshot"] is False
    assert isolation["secrets_excluded"] is True


def test_reports_are_written_outside_the_repository(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01", write_reports=True)
    assert out.report_error is None
    assert out.json_path and out.markdown_path
    reports = tmp_path / "reports"
    assert reports.is_dir()
    written = list(reports.rglob("report.json"))
    assert written
    for path in written:
        assert not path.is_relative_to(out.root)
    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["scan_id"] == out.scan_id
    assert not (out.root / "report.json").exists()


def test_markdown_contains_coverage_and_sections(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01", write_reports=True)
    with open(out.markdown_path, encoding="utf-8") as handle:
        markdown = handle.read()
    assert "# Dead Code Hunter report" in markdown
    assert "## Coverage" in markdown
    assert "## Isolation" in markdown
    assert "never proof that code is safe to delete" in markdown
    assert "## Repository integrity" in markdown


def test_report_location_inside_the_repository_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        report_dir(repo, "scan", base=repo / "reports")


def test_default_reports_base_is_outside_any_repo():
    base = default_reports_base()
    assert "dead-code-hunter" in str(base).lower()


def test_summary_counts_match_findings(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01")
    summary = out.report["summary"]
    assert summary["findings_total"] == len(out.report["findings"])
    assert sum(summary["by_recommendation"].values()) == summary["findings_total"]
    assert summary["ignored_total"] == len(out.report["ignored"])


def test_two_scans_produce_the_same_findings(tmp_path):
    first = run_fixture(tmp_path / "one", "F-PY-01")
    second = run_fixture(tmp_path / "two", "F-PY-01")
    projection = lambda out: sorted(  # noqa: E731
        (
            f.file,
            f.line,
            f.symbol,
            f.kind,
            f.recommendation.value,
            f.rule.value,
        )
        for f in out.findings
    )
    assert projection(first) == projection(second)


def test_determinism_across_languages(tmp_path):
    names = ("F-JS-01", "F-DT-01")
    results = []
    for attempt in range(2):
        collected = []
        for name in names:
            out = run_fixture(tmp_path / f"{name}-{attempt}", name)
            collected.extend(
                (name, f.file, f.line, f.symbol, f.kind, f.recommendation.value, f.rule.value)
                for f in out.findings
            )
        results.append(sorted(collected))
    assert results[0] == results[1]


def test_fingerprints_are_stable_and_unique(tmp_path):
    out = run_fixture(tmp_path, "F-PY-01")
    fingerprints = [f.fingerprint for f in out.findings]
    assert len(fingerprints) == len(set(fingerprints))
    again = run_fixture(tmp_path / "again", "F-PY-01")
    assert [f.fingerprint for f in again.findings] == fingerprints
