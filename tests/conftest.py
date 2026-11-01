"""Shared test helpers: materializing fixtures and controlling analyzer output."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from dead_code_hunter.models import Finding
from dead_code_hunter.scanner import ScanOptions, ScanOutcome, scan
from dead_code_hunter.tools import Command, ToolOverrides

from .fixtures_repo import FIXTURES, TOOL_OUTPUT

STUB = Path(__file__).parent / "stubs" / "fake_tool.py"


def materialize(base: Path, name: str) -> Path:
    """Write a fixture repository under ``base`` and return its root."""
    if name not in FIXTURES:
        raise KeyError(f"unknown fixture {name}")
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for rel, content in FIXTURES[name].items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


def stub_command(workdir: Path, tool: str, config: dict) -> Command:
    config_path = workdir / f"stub-{tool}-{uuid.uuid4().hex}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return Command(sys.executable, (str(STUB), str(config_path)), tool=tool)


def overrides_for(workdir: Path, name: str, skip: tuple[str, ...] = ()) -> ToolOverrides:
    overrides = ToolOverrides()
    for tool, config in TOOL_OUTPUT.get(name, {}).items():
        if tool in skip:
            continue
        overrides.commands[tool] = stub_command(workdir, tool, config)
    overrides.disabled.update(skip)
    return overrides


def run_fixture(
    tmp_path: Path,
    name: str,
    *,
    langs: tuple[str, ...] | None = None,
    overrides: ToolOverrides | None = None,
    skip_tools: tuple[str, ...] = (),
    write_reports: bool = False,
    extra_adapters: list | None = None,
    scope: list[str] | None = None,
    repo: Path | None = None,
) -> ScanOutcome:
    repo = repo or materialize(tmp_path, name)
    options = ScanOptions(
        root=repo,
        langs=langs or ("python", "js", "dart"),
        scope=scope or [],
        overrides=overrides if overrides is not None else overrides_for(tmp_path, name, skip_tools),
        write_reports=write_reports,
        reports_base=tmp_path / "reports",
        extra_adapters=extra_adapters or [],
        scan_id="test-scan-0001",
    )
    return scan(options)


def one(outcome: ScanOutcome, symbol: str) -> Finding:
    matches = [f for f in outcome.findings if f.symbol == symbol]
    assert matches, f"no finding for symbol {symbol!r}; got {[f.symbol for f in outcome.findings]}"
    assert len(matches) == 1, f"expected exactly one finding for {symbol!r}, got {len(matches)}"
    return matches[0]


def by_file(outcome: ScanOutcome, path: str) -> Finding:
    matches = [f for f in outcome.findings if f.file == path]
    assert matches, f"no finding for file {path!r}; got {[f.file for f in outcome.findings]}"
    return matches[0]


def coverage_of(outcome: ScanOutcome, tool: str, project: str | None = None) -> dict:
    for entry in outcome.report["coverage"]:
        if entry["tool"] == tool and (project is None or entry["project"] == project):
            return entry
    raise AssertionError(f"no coverage entry for {tool}: {outcome.report['coverage']}")


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    return tmp_path
