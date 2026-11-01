"""Deterministic recommendation rules R1-R8 (spec "Κανόνες σύστασης").

This module is intentionally analyzer-agnostic: it only reads the four axes
and the reference outcome from a :class:`~dead_code_hunter.models.Finding`.
Adding a new analyzer never requires touching this file.
"""

from __future__ import annotations

from .models import (
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


def classify(finding: Finding) -> tuple[Recommendation, Rule, str]:
    """Apply R1-R8 in order; the first matching rule wins."""
    if finding.category is not Category.DEAD_CODE:  # R1
        return (
            Recommendation.INFO,
            Rule.R1,
            f"category '{finding.category.value}' is not dead_code; never on a deletion path",
        )
    if finding.contract is Contract.PUBLIC:  # R2
        return (
            Recommendation.PROTECTED,
            Rule.R2,
            "public contract: " + ("; ".join(finding.contract_reasons) or "public surface"),
        )
    if finding.reference is ReferenceKind.ACTIVE:  # R3
        return (
            Recommendation.RETAIN,
            Rule.R3,
            "confirmed active reference: " + ("; ".join(finding.reference_reasons) or "reference found"),
        )
    if finding.coverage is not Coverage.COMPLETE:  # R4
        reasons = "; ".join(finding.coverage_reasons) or "analysis not complete"
        return (
            Recommendation.REVIEW_REQUIRED,
            Rule.R4,
            f"coverage {finding.coverage.value if finding.coverage else 'unknown'}: {reasons}",
        )
    if finding.contract is Contract.INTERNAL_SHARED:  # R5
        return (
            Recommendation.REVIEW_REQUIRED,
            Rule.R5,
            "internal shared surface: "
            + ("; ".join(finding.contract_reasons) or "possibly used elsewhere"),
        )
    if finding.impact in (Impact.HIGH, Impact.CRITICAL):  # R6
        return (
            Recommendation.REVIEW_REQUIRED,
            Rule.R6,
            f"impact {finding.impact.value}: " + ("; ".join(finding.impact_reasons) or "high cost"),
        )
    if finding.confidence is Confidence.LOW or finding.reference is ReferenceKind.UNRESOLVED:  # R7
        if finding.confidence is Confidence.LOW:
            reason = "low confidence: " + (
                "; ".join(finding.confidence_reasons) or "weak evidence"
            )
        else:
            reason = "unresolved reference: " + (
                "; ".join(finding.reference_reasons) or "name appears elsewhere"
            )
        return (Recommendation.REVIEW_REQUIRED, Rule.R7, reason)
    return (  # R8
        Recommendation.CANDIDATE,
        Rule.R8,
        "private, unused code with complete coverage (strong candidate, never 'safe')",
    )


def apply_classification(finding: Finding) -> Finding:
    recommendation, rule, reason = classify(finding)
    finding.recommendation = recommendation
    finding.rule = rule
    finding.rule_reason = reason
    return finding


RECOMMENDATION_ORDER = (
    Recommendation.PROTECTED,
    Recommendation.RETAIN,
    Recommendation.REVIEW_REQUIRED,
    Recommendation.CANDIDATE,
    Recommendation.INFO,
)
