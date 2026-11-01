"""Cross-cutting fixtures F-X-01, F-X-02a, F-X-04, F-X-05."""

from __future__ import annotations

import json

from dead_code_hunter.config import load_config, load_ignore
from dead_code_hunter.models import Confidence, Recommendation, ReferenceKind, Rule

from .conftest import materialize, one, run_fixture


def test_f_x_01_secret_files_are_never_read_and_never_reported(tmp_path):
    out = run_fixture(tmp_path, "F-X-01")
    payload = json.dumps(out.report, ensure_ascii=False)
    assert "SECRET-SENTINEL-DO-NOT-LEAK" not in payload
    assert out.report["isolation"]["secrets_excluded"] is True
    assert out.report["discovery"]["skipped_secret_files"] >= 2


def test_f_x_02a_analyzer_writing_into_the_repo_is_detected(tmp_path):
    repo = materialize(tmp_path, "F-X-02a")
    # Turn the repo into a real git checkout so status can be compared.
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "tests")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")

    from .conftest import stub_command

    marker = repo / "app" / "written_by_analyzer.py"
    overrides = {
        "ruff": {"exit": 0, "stdout": "[]"},
        "vulture": {"exit": 0, "stdout": "", "touch": [str(marker)]},
    }
    from dead_code_hunter.tools import ToolOverrides

    tools = ToolOverrides()
    for tool, config in overrides.items():
        tools.commands[tool] = stub_command(tmp_path, tool, config)

    out = run_fixture(tmp_path, "F-X-02a", overrides=tools, repo=repo)
    assert marker.is_file(), "the simulated analyzer must have written the file"
    assert out.report["git_integrity"]["changed"] is True
    assert out.exit_code != 0
    assert any("written_by_analyzer" in path for path in out.report["git_integrity"]["changed_files"])


def test_f_x_04_common_word_is_low_confidence_without_signal_flood(tmp_path):
    out = run_fixture(tmp_path, "F-X-04")
    finding = one(out, "name")
    assert finding.confidence is Confidence.LOW
    assert finding.reference is ReferenceKind.NONE
    assert finding.reference_reasons == ["no reference found in the scanned scope"]
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R7


def test_f_x_05_ignored_findings_are_reported_with_their_rule(tmp_path):
    out = run_fixture(tmp_path, "F-X-05")
    assert not [f for f in out.findings if f.symbol == "calculate_internal_margin"]
    assert len(out.report["ignored"]) == 1
    entry = out.report["ignored"][0]
    assert entry["rule"] == "app/core.py::calculate_internal_margin"
    assert entry["finding"]["symbol"] == "calculate_internal_margin"


def test_x_5_uncommitted_changes_are_an_informational_signal_only(tmp_path):
    import subprocess

    repo = materialize(tmp_path, "F-PY-01")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True, capture_output=True)
    (repo / "app" / "core.py").write_text(
        "def calculate_internal_margin(base, rate):\n    return base * rate\n\n\ndef changed():\n    return 2\n",
        encoding="utf-8",
    )

    out = run_fixture(tmp_path, "F-PY-01", repo=repo)
    finding = one(out, "calculate_internal_margin")
    assert "uncommitted-changes" in finding.signals
    assert finding.recommendation is Recommendation.CANDIDATE  # X-5 must not change it


def test_ignore_file_parsing_reports_errors(tmp_path):
    (tmp_path / ".dead-code").mkdir()
    (tmp_path / ".dead-code" / "ignore").write_text("# comment\n\n::broken\nok/path.py::symbol\n")
    ignore = load_ignore(tmp_path)
    assert ignore.parse_errors
    assert len(ignore.rules) == 1


def test_config_unknown_keys_are_reported_not_obeyed(tmp_path):
    (tmp_path / ".dead-code").mkdir()
    (tmp_path / ".dead-code" / "config.toml").write_text(
        '[impact]\nhigh = ["billing"]\nunknown = ["x"]\n[scan]\ntimeout_seconds = 30\nrun = "rm -rf /"\n'
    )
    config = load_config(tmp_path)
    assert "impact.unknown" in config.unknown_keys
    assert "scan.run" in config.unknown_keys
    assert config.timeout_seconds == 30
    assert config.impact.high == ("billing",)


def test_config_parse_error_is_surfaced_and_defaults_are_used(tmp_path):
    (tmp_path / ".dead-code").mkdir()
    (tmp_path / ".dead-code" / "config.toml").write_text("[impact\nbroken")
    config = load_config(tmp_path)
    assert config.parse_error
    assert "billing" in config.impact.critical
