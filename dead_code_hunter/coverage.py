"""Coverage aggregation.

A finding can only reach CANDIDATE when the analysis behind it is COMPLETE
(spec R4).  This module combines the tool-level coverage verdict with the
per-finding caveats the adapters and reference analyses produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Coverage

_SEVERITY = {
    Coverage.COMPLETE: 0,
    Coverage.NOT_APPLICABLE: 0,
    Coverage.PARTIAL: 1,
    Coverage.FAILED: 2,
}


def worst(*values: Coverage) -> Coverage:
    result = Coverage.COMPLETE
    for value in values:
        if _SEVERITY[value] > _SEVERITY[result]:
            result = value
    return result


@dataclass
class CoverageEntry:
    project: str
    lang: str
    tool: str
    coverage: Coverage
    reason: str | None = None
    tool_path: str | None = None
    tool_version: str | None = None
    exit_code: int | None = None
    command: list[str] | None = None
    inherited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "lang": self.lang,
            "tool": self.tool,
            "coverage": self.coverage.value,
            "reason": self.reason,
            "tool_path": self.tool_path,
            "tool_version": self.tool_version,
            "exit_code": self.exit_code,
            "command": self.command,
            "inherited": self.inherited,
        }


@dataclass
class CoverageRegistry:
    entries: list[CoverageEntry] = field(default_factory=list)

    def add(self, entry: CoverageEntry) -> None:
        self.entries.append(entry)

    def add_result(self, result: Any) -> None:
        self.entries.append(
            CoverageEntry(
                project=result.project,
                lang=result.lang,
                tool=result.tool,
                coverage=result.coverage,
                reason=result.coverage_reason,
                tool_path=result.tool_path,
                tool_version=result.tool_version,
                exit_code=result.exit_code,
                command=result.command,
            )
        )

    def summary(self) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            key = f"{entry.project}::{entry.tool}"
            summary[key] = entry.to_dict()
        return summary

    def to_list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]
