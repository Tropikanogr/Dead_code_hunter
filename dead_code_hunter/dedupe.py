"""Finding deduplication.

Two tools may report the same item (for example ruff and vulture both noticing
an unused import).  The merged finding keeps the strongest evidence and records
the corroborating tool in ``signals``; the merge is order-independent so two
runs always produce the same set.
"""

from __future__ import annotations

from .models import Category, Confidence, Contract, Coverage, Finding, Impact, ReferenceKind

_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
_CONTRACT_RANK = {Contract.NONE: 0, Contract.INTERNAL_SHARED: 1, Contract.PUBLIC: 2}
_IMPACT_RANK = {Impact.LOW: 0, Impact.MEDIUM: 1, Impact.HIGH: 2, Impact.CRITICAL: 3}
#: Coverage is merged by *severity*: the weakest verdict wins.  A tool that
#: finished cleanly cannot vouch for one that did not (spec R4, P1).
_COVERAGE_SEVERITY = {
    Coverage.COMPLETE: 0,
    Coverage.NOT_APPLICABLE: 1,
    Coverage.PARTIAL: 2,
    Coverage.FAILED: 3,
}
_REFERENCE_RANK = {ReferenceKind.NONE: 0, ReferenceKind.UNRESOLVED: 1, ReferenceKind.ACTIVE: 2}


def dedupe_key(finding: Finding) -> tuple:
    """Identity of one item.

    The line is part of the key: two same-named members on different lines are
    different items, while two tools reporting the same line collapse.
    """
    return (
        finding.file,
        finding.symbol or "",
        finding.kind,
        finding.category.value,
        finding.line if finding.line is not None else -1,
    )


def _best(current, candidate, rank_map):
    if current is None:
        return candidate
    if candidate is None:
        return current
    return candidate if rank_map[candidate] > rank_map[current] else current


def merge(group: list[Finding]) -> Finding:
    """Merge findings with the same identity into one, strongest evidence wins."""
    ordered = sorted(
        group,
        key=lambda f: (f.tool, f.line if f.line is not None else -1, f.message),
    )
    base = ordered[0]
    merged = Finding(
        tool=base.tool,
        lang=base.lang,
        project=base.project,
        kind=base.kind,
        category=base.category,
        file=base.file,
        line=min((f.line for f in ordered if f.line is not None), default=None),
        end_line=next((f.end_line for f in ordered if f.end_line is not None), None),
        symbol=base.symbol,
        message=base.message,
    )
    merged.signals = sorted({s for f in ordered for s in f.signals})
    merged.confidence_reasons = sorted({r for f in ordered for r in f.confidence_reasons})
    merged.contract_reasons = sorted({r for f in ordered for r in f.contract_reasons})
    merged.impact_reasons = sorted({r for f in ordered for r in f.impact_reasons})
    merged.coverage_reasons = sorted({r for f in ordered for r in f.coverage_reasons})
    merged.reference_reasons = sorted({r for f in ordered for r in f.reference_reasons})

    for finding in ordered:
        merged.confidence = _best(merged.confidence, finding.confidence, _CONFIDENCE_RANK)
        merged.contract = _best(merged.contract, finding.contract, _CONTRACT_RANK)
        merged.impact = _best(merged.impact, finding.impact, _IMPACT_RANK)
        merged.coverage = _best(merged.coverage, finding.coverage, _COVERAGE_SEVERITY)
        merged.reference = _best(merged.reference, finding.reference, _REFERENCE_RANK)

    tools = sorted({f.tool for f in ordered})
    merged.tool = tools[0]
    if len(tools) > 1:
        merged.signals = sorted(set(merged.signals) | {f"corroborated by: {', '.join(tools[1:])}"})
    if any(f.category is Category.DEAD_CODE for f in ordered):
        merged.category = Category.DEAD_CODE
    # Identity survives the merge: approvals, reports and re-scans all key on it.
    merged.scan_id = next((f.scan_id for f in ordered if f.scan_id), "")
    merged.fingerprint = merged.compute_fingerprint()
    merged.id = merged.compute_id()
    return merged


def dedupe(findings: list[Finding]) -> tuple[list[Finding], int]:
    """Return deduplicated findings (deterministic order) and the merge count."""
    groups: dict[tuple, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(dedupe_key(finding), []).append(finding)
    merged = [merge(group) for group in groups.values()]
    merged.sort(
        key=lambda f: (
            f.file,
            f.line if f.line is not None else -1,
            f.symbol or "",
            f.kind,
            f.tool,
        )
    )
    removed = sum(len(group) - 1 for group in groups.values())
    return merged, removed
