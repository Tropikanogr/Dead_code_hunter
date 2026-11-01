"""Adapter failure modes, exit-code handling and the AD-1 contract."""

from __future__ import annotations

import pytest

from dead_code_hunter.adapters.base import Adapter, AdapterContext
from dead_code_hunter.models import (
    AnalyzerResult,
    Category,
    Coverage,
    Finding,
    Recommendation,
    Rule,
)
from dead_code_hunter.tools import ToolOverrides

from .conftest import coverage_of, stub_command
from .fixtures_repo import pyproject
from .conftest import run_fixture


def python_repo(tmp_path, name: str = "repo") -> "object":
    repo = tmp_path / name
    (repo / "app").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(pyproject("dch-adapters"), encoding="utf-8")
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "core.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return repo


def overrides(tmp_path, **tools) -> ToolOverrides:
    result = ToolOverrides()
    for tool, config in tools.items():
        result.commands[tool] = stub_command(tmp_path, tool, config)
    return result


@pytest.mark.parametrize("exit_code", [0, 3])
def test_vulture_success_exit_codes_are_complete(tmp_path, exit_code):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path,
            ruff={"exit": 0, "stdout": "[]"},
            vulture={"exit": exit_code, "stdout": ""},
        ),
    )
    assert coverage_of(out, "vulture")["coverage"] == Coverage.COMPLETE.value


def test_vulture_exit_1_is_partial_and_names_the_file(tmp_path):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path,
            ruff={"exit": 0, "stdout": "[]"},
            vulture={"exit": 1, "stdout": "", "stderr": "app/core.py:1: error: invalid syntax\n"},
        ),
    )
    entry = coverage_of(out, "vulture")
    assert entry["coverage"] == Coverage.PARTIAL.value
    assert "app/core.py" in entry["reason"]


def test_vulture_exit_2_is_failed(tmp_path):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 2, "stderr": "boom"}
        ),
    )
    assert coverage_of(out, "vulture")["coverage"] == Coverage.FAILED.value


def test_ruff_failure_is_failed(tmp_path):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path,
            ruff={"exit": 2, "stdout": "", "stderr": "error: invalid pyproject"},
            vulture={"exit": 0, "stdout": ""},
        ),
    )
    entry = coverage_of(out, "ruff")
    assert entry["coverage"] == Coverage.FAILED.value
    assert "2" in entry["reason"]


def test_ruff_missing_is_failed_and_explained(tmp_path):
    repo = python_repo(tmp_path)
    overrides_obj = overrides(tmp_path, vulture={"exit": 0, "stdout": ""})
    overrides_obj.disabled.add("ruff")
    out = run_fixture(tmp_path, "F-PY-01", repo=repo, overrides=overrides_obj)
    entry = coverage_of(out, "ruff")
    assert entry["coverage"] == Coverage.FAILED.value
    assert entry["tool_path"] is None


def test_knip_partial_exit_with_parseable_output_is_partial(tmp_path):
    out = run_fixture(
        tmp_path,
        "F-JS-01",
        overrides=overrides(tmp_path, knip={"exit": 2, "stdout": '{"files": [], "issues": []}'}),
    )
    entry = coverage_of(out, "knip")
    assert entry["coverage"] == Coverage.PARTIAL.value
    assert "2" in entry["reason"]


def test_knip_error_without_json_is_failed(tmp_path):
    out = run_fixture(
        tmp_path,
        "F-JS-01",
        overrides=overrides(tmp_path, knip={"exit": 2, "stdout": "", "stderr": "Cannot find module"}),
    )
    assert coverage_of(out, "knip")["coverage"] == Coverage.FAILED.value


def test_dart_exit_2_without_diagnostics_is_failed(tmp_path):
    out = run_fixture(
        tmp_path,
        "F-DT-01",
        overrides=overrides(tmp_path, dart={"exit": 2, "stdout": "", "stderr": "tool crashed"}),
    )
    assert coverage_of(out, "dart analyze")["coverage"] == Coverage.FAILED.value


def test_dart_compile_error_is_partial(tmp_path):
    diagnostic = "ERROR|COMPILE_TIME_ERROR|undefined_identifier|lib/main.dart|3|9|4|Undefined name 'x'.\n"
    out = run_fixture(
        tmp_path,
        "F-DT-01",
        overrides=overrides(tmp_path, dart={"exit": 2, "stdout": diagnostic}),
    )
    entry = coverage_of(out, "dart analyze")
    assert entry["coverage"] == Coverage.PARTIAL.value
    assert "lib/main.dart" in entry["reason"]


class FakeAnalyzer(Adapter):
    """Same contract, unknown tool: must work without touching the classifier."""

    name = "fake-analyzer"
    lang = "python"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        return AnalyzerResult(
            tool=self.name,
            lang=self.lang,
            project=ctx.project,
            coverage=Coverage.COMPLETE,
            coverage_reason="simulated analyzer",
            findings=[
                Finding(
                    tool=self.name,
                    lang=self.lang,
                    project=ctx.project,
                    kind="function",
                    category=Category.DEAD_CODE,
                    file="app/core.py",
                    line=5,
                    symbol="simulated_unused",
                    message="fake analyzer says unused",
                )
            ],
        )


class BrokenAnalyzer(Adapter):
    name = "broken-analyzer"
    lang = "python"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        raise RuntimeError("analyzer exploded")


def test_third_party_adapter_fits_the_contract(tmp_path):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
        ),
        extra_adapters=[FakeAnalyzer],
    )
    finding = next(f for f in out.findings if f.symbol == "simulated_unused")
    assert finding.tool == "fake-analyzer"
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8
    assert any(entry["tool"] == "fake-analyzer" for entry in out.report["coverage"])


def test_adapter_exception_is_recorded_as_failed_never_successful(tmp_path):
    repo = python_repo(tmp_path)
    out = run_fixture(
        tmp_path,
        "F-PY-01",
        repo=repo,
        overrides=overrides(
            tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
        ),
        extra_adapters=[BrokenAnalyzer],
    )
    entry = coverage_of(out, "broken-analyzer")
    assert entry["coverage"] == Coverage.FAILED.value
    assert "analyzer exploded" in entry["reason"]
