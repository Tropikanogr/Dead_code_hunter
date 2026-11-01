"""R1-R8 classification is deterministic and independent of any analyzer."""

from __future__ import annotations

import pytest

from dead_code_hunter.classify import apply_classification, classify
from dead_code_hunter.models import (
    Category,
    Confidence,
    Contract,
    Coverage,
    Finding,
    Impact,
    Recommendation,
    ReferenceKind,
    Rule,
)


def make(**kwargs) -> Finding:
    defaults = dict(
        tool="any-analyzer",
        lang="python",
        project=".",
        kind="function",
        category=Category.DEAD_CODE,
        file="app/core.py",
        line=10,
        symbol="helper",
        confidence=Confidence.MEDIUM,
        contract=Contract.NONE,
        impact=Impact.LOW,
        coverage=Coverage.COMPLETE,
        reference=ReferenceKind.NONE,
    )
    defaults.update(kwargs)
    return Finding(**defaults)


def test_r1_non_dead_code_is_info():
    finding = apply_classification(make(category=Category.DEPENDENCY_ISSUE))
    assert finding.recommendation is Recommendation.INFO
    assert finding.rule is Rule.R1


def test_r2_public_contract_is_protected():
    finding = apply_classification(make(contract=Contract.PUBLIC))
    assert finding.recommendation is Recommendation.PROTECTED
    assert finding.rule is Rule.R2


def test_r3_active_reference_is_retained():
    finding = apply_classification(make(reference=ReferenceKind.ACTIVE, coverage=Coverage.PARTIAL))
    assert finding.recommendation is Recommendation.RETAIN
    assert finding.rule is Rule.R3


def test_r4_incomplete_coverage_requires_review():
    finding = apply_classification(make(coverage=Coverage.PARTIAL))
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R4
    failed = apply_classification(make(coverage=Coverage.FAILED))
    assert failed.recommendation is Recommendation.REVIEW_REQUIRED


def test_r5_internal_shared_requires_review():
    finding = apply_classification(make(contract=Contract.INTERNAL_SHARED))
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R5


def test_r6_high_impact_requires_review():
    finding = apply_classification(make(impact=Impact.CRITICAL))
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule is Rule.R6


def test_r7_low_confidence_or_unresolved_requires_review():
    low = apply_classification(make(confidence=Confidence.LOW))
    assert low.rule is Rule.R7 and low.recommendation is Recommendation.REVIEW_REQUIRED
    unresolved = apply_classification(make(reference=ReferenceKind.UNRESOLVED))
    assert unresolved.rule is Rule.R7 and unresolved.recommendation is Recommendation.REVIEW_REQUIRED


def test_r8_private_unused_with_complete_coverage_is_candidate():
    finding = apply_classification(make())
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.rule is Rule.R8
    assert "never 'safe'" in finding.rule_reason


def test_rule_order_is_fixed():
    """R2 beats R4..R7; R3 beats R4..R7 (spec AC-CL-3 / AC-CL-4)."""
    public_low = classify(make(contract=Contract.PUBLIC, confidence=Confidence.LOW, impact=Impact.CRITICAL))
    assert public_low[0] is Recommendation.PROTECTED
    active_low = classify(
        make(reference=ReferenceKind.ACTIVE, confidence=Confidence.LOW, coverage=Coverage.FAILED)
    )
    assert active_low[0] is Recommendation.RETAIN


@pytest.mark.parametrize("contract", [Contract.PUBLIC])
@pytest.mark.parametrize("coverage", list(Coverage))
@pytest.mark.parametrize("confidence", list(Confidence))
@pytest.mark.parametrize("impact", list(Impact))
@pytest.mark.parametrize("reference", list(ReferenceKind))
def test_no_public_finding_ever_reaches_candidate_or_review(
    contract, coverage, confidence, impact, reference
):
    recommendation, rule, _ = classify(
        make(contract=contract, coverage=coverage, confidence=confidence, impact=impact, reference=reference)
    )
    assert recommendation not in (Recommendation.CANDIDATE, Recommendation.REVIEW_REQUIRED)
    assert rule is Rule.R2


@pytest.mark.parametrize("coverage", list(Coverage))
@pytest.mark.parametrize("confidence", list(Confidence))
@pytest.mark.parametrize("impact", list(Impact))
def test_confirmed_reference_never_reaches_review_or_candidate(coverage, confidence, impact):
    recommendation, rule, _ = classify(
        make(
            reference=ReferenceKind.ACTIVE,
            coverage=coverage,
            confidence=confidence,
            impact=impact,
            contract=Contract.NONE,
        )
    )
    assert recommendation is Recommendation.RETAIN
    assert rule is Rule.R3


def test_classification_is_deterministic():
    inputs = [
        make(symbol=f"symbol_{index}", impact=Impact(impact))
        for index, impact in enumerate(Impact)
    ]
    first = [(f.symbol, classify(f)[:2]) for f in inputs]
    second = [(f.symbol, classify(f)[:2]) for f in inputs]
    assert first == second


def test_every_finding_gets_rule_and_per_axis_reasons(tmp_path):
    finding = make(
        confidence_reasons=["because"],
        contract_reasons=["reason"],
        impact_reasons=["keyword"],
        coverage_reasons=["partial tool"],
        reference_reasons=["imported"],
    )
    apply_classification(finding)
    payload = finding.to_dict()
    assert payload["recommendation"] and payload["rule"]
    for key in (
        "confidence_reasons",
        "contract_reasons",
        "impact_reasons",
        "coverage_reasons",
        "reference_reasons",
    ):
        assert payload[key]
