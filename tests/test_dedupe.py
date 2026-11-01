"""Deduplication and merge semantics."""

from __future__ import annotations

from dead_code_hunter.dedupe import dedupe
from dead_code_hunter.models import (
    Category,
    Confidence,
    Contract,
    Coverage,
    Finding,
    ReferenceKind,
)


def make(**kwargs) -> Finding:
    defaults = dict(
        tool="ruff",
        lang="python",
        project=".",
        kind="import",
        category=Category.DEAD_CODE,
        file="app/utils.py",
        line=1,
        symbol="json",
        confidence=Confidence.HIGH,
        contract=Contract.NONE,
        impact=None,
        coverage=Coverage.COMPLETE,
        reference=ReferenceKind.NONE,
    )
    defaults.update(kwargs)
    return Finding(**defaults)


def test_identical_findings_are_merged_and_corroboration_is_recorded():
    ruff = make(tool="ruff")
    vulture = make(tool="vulture", confidence=Confidence.MEDIUM, signals=["vulture:import"])
    merged, removed = dedupe([ruff, vulture])
    assert removed == 1
    assert len(merged) == 1
    finding = merged[0]
    assert finding.confidence is Confidence.HIGH  # strongest evidence wins
    assert any("corroborated by" in signal for signal in finding.signals)


def test_order_does_not_change_the_result():
    a = make(tool="ruff")
    b = make(tool="vulture", confidence=Confidence.MEDIUM)
    first, _ = dedupe([a, b])
    second, _ = dedupe([b, a])
    assert first[0].to_dict()["confidence"] == second[0].to_dict()["confidence"]
    assert first[0].tool == second[0].tool


def test_different_symbols_are_not_merged():
    merged, removed = dedupe([make(symbol="json"), make(symbol="os")])
    assert removed == 0
    assert len(merged) == 2


def test_category_is_part_of_the_finding_identity():
    """An INFO item from one tool never hides a dead_code item from another."""
    dead = make(category=Category.DEAD_CODE)
    info = make(category=Category.INFO, tool="knip")
    merged, removed = dedupe([dead, info])
    assert removed == 0
    assert {f.category for f in merged} == {Category.DEAD_CODE, Category.INFO}


def test_order_is_deterministic_by_file_and_line():
    findings = [
        make(file="b.py", line=2),
        make(file="a.py", line=9),
        make(file="a.py", line=1),
    ]
    merged, _ = dedupe(findings)
    assert [(f.file, f.line) for f in merged] == [("a.py", 1), ("a.py", 9), ("b.py", 2)]
