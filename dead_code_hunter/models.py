"""Normalized data model shared by every analyzer, the classifier and the reporters.

The model deliberately knows nothing about which tool produced a finding: an
analyzer adapter fills :class:`AnalyzerResult`, and enrichment fills the
evidence fields.  The classification engine (``classify``) only consumes
:class:`Finding` objects.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Coverage(str, Enum):
    """How complete the analysis behind a finding is."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Confidence(str, Enum):
    """Strength of the evidence that a symbol is unused.  Not a probability."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Contract(str, Enum):
    """Whether removing the symbol can break a contract with someone else."""

    NONE = "NONE"
    INTERNAL_SHARED = "INTERNAL_SHARED"
    PUBLIC = "PUBLIC"


class Impact(str, Enum):
    """Cost of a wrong decision."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Recommendation(str, Enum):
    CANDIDATE = "CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RETAIN = "RETAIN"
    PROTECTED = "PROTECTED"
    INFO = "INFO"


class Rule(str, Enum):
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"
    R5 = "R5"
    R6 = "R6"
    R7 = "R7"
    R8 = "R8"


class Category(str, Enum):
    """Finding category.  Only ``DEAD_CODE`` can ever reach CANDIDATE."""

    DEAD_CODE = "dead_code"
    DEPENDENCY_ISSUE = "dependency_issue"
    INFO = "info"


class ReferenceKind(str, Enum):
    """Outcome of the repository-wide reference analysis for one finding."""

    ACTIVE = "active"
    UNRESOLVED = "unresolved"
    NONE = "none"


LANGUAGES = ("python", "js", "dart")

#: Names that carry no evidential weight on their own (spec X-3, F-X-04).
COMMON_WORDS: frozenset[str] = frozenset(
    {
        "name", "data", "id", "status", "value", "type", "key", "item", "items",
        "result", "results", "index", "size", "count", "text", "label", "tag",
        "tags", "title", "body", "content", "config", "options", "params",
        "kwargs", "args", "payload", "meta", "info", "error", "message", "msg",
        "run", "start", "stop", "init", "setup", "update", "handle", "process",
        "main", "test", "get", "set", "add", "remove", "create", "load", "save",
    }
)


def is_common_word(symbol: str | None) -> bool:
    if not symbol:
        return False
    return symbol.lower() in COMMON_WORDS


def normalize_path(path: str) -> str:
    """Repo-relative POSIX path with no ``./`` prefix."""
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned or "."


@dataclass
class AnalyzerResult:
    """Common adapter contract (spec X-6 / AD-1).

    Every adapter returns normalized ``findings`` plus a coverage verdict with
    a reason.  Nothing in this object references a tool-specific payload, so
    the classifier can consume any adapter output unchanged.
    """

    tool: str
    lang: str
    project: str
    coverage: Coverage
    coverage_reason: str | None = None
    findings: list["Finding"] = field(default_factory=list)
    tool_path: str | None = None
    tool_version: str | None = None
    command: list[str] | None = None
    exit_code: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Facts the adapter needs to hand to the shared enrichment stage.
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "lang": self.lang,
            "project": self.project,
            "coverage": self.coverage.value,
            "coverage_reason": self.coverage_reason,
            "tool_path": self.tool_path,
            "tool_version": self.tool_version,
            "command": self.command,
            "exit_code": self.exit_code,
            "diagnostics": self.diagnostics,
            "metadata": self.metadata,
        }


@dataclass
class Finding:
    """One normalized candidate item, before and after evidence enrichment."""

    tool: str
    lang: str
    project: str
    kind: str
    category: Category
    file: str
    line: int | None = None
    end_line: int | None = None
    symbol: str | None = None
    message: str = ""
    # Enrichment (filled by reference/contract/impact analysis).
    confidence: Confidence | None = None
    confidence_reasons: list[str] = field(default_factory=list)
    contract: Contract | None = None
    contract_reasons: list[str] = field(default_factory=list)
    impact: Impact | None = None
    impact_reasons: list[str] = field(default_factory=list)
    coverage: Coverage | None = None
    coverage_reasons: list[str] = field(default_factory=list)
    reference: ReferenceKind | None = None
    reference_reasons: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    # Classification.
    recommendation: Recommendation | None = None
    rule: Rule | None = None
    rule_reason: str = ""
    # Identity.
    id: str = ""
    scan_id: str = ""
    fingerprint: str = ""

    def identity(self) -> tuple[str, int, str, str, str, str]:
        return (
            self.file,
            self.line if self.line is not None else -1,
            self.symbol or "",
            self.kind,
            self.category.value,
            self.tool,
        )

    def compute_fingerprint(self) -> str:
        raw = "|".join(
            [
                self.lang,
                self.project,
                self.file.replace("\\", "/"),
                str(self.line if self.line is not None else ""),
                self.symbol or "",
                self.kind,
                self.category.value,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def compute_id(self) -> str:
        base = self.symbol or self.file
        slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()[:40] or "finding"
        return f"{self.lang}-{slug}-{self.fingerprint[:8]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scan_id": self.scan_id,
            "lang": self.lang,
            "project": self.project,
            "tool": self.tool,
            "kind": self.kind,
            "category": self.category.value,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "symbol": self.symbol,
            "confidence": self.confidence.value if self.confidence else None,
            "confidence_reasons": self.confidence_reasons,
            "contract_sensitivity": self.contract.value if self.contract else None,
            "contract_reasons": self.contract_reasons,
            "impact": self.impact.value if self.impact else None,
            "impact_reasons": self.impact_reasons,
            "coverage": self.coverage.value if self.coverage else None,
            "coverage_reasons": self.coverage_reasons,
            "reference": self.reference.value if self.reference else None,
            "reference_reasons": self.reference_reasons,
            "signals": self.signals,
            "recommendation": self.recommendation.value if self.recommendation else None,
            "rule": self.rule.value if self.rule else None,
            "rule_reason": self.rule_reason,
            "fingerprint": self.fingerprint,
            "external_consumers_checked": {"supported": False, "repos": []},
            "evidence": {},
        }


@dataclass
class IgnoredFinding:
    finding: Finding
    rule: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "finding": self.finding.to_dict()}


@dataclass
class ProjectInfo:
    path: str
    langs: list[str]
    markers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "langs": self.langs,
            "markers": self.markers,
            "metadata": self.metadata,
        }
