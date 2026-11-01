"""Shared adapter contract (spec X-6 / AD-1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ScannerConfig
from ..models import (
    AnalyzerResult,
    Coverage,
    Finding,
    normalize_path,
)
from ..tools import ToolOverrides


@dataclass
class AdapterContext:
    root: Path
    project: str  # repo-relative project path ("." for the repo root)
    repo_files: list[str]
    config: ScannerConfig
    overrides: ToolOverrides = field(default_factory=ToolOverrides)

    @property
    def project_root(self) -> Path:
        return self.root if self.project in (".", "") else self.root / self.project

    @property
    def timeout(self) -> int:
        return self.config.timeout_seconds

    def rel(self, path: str) -> str:
        """Normalize an analyzer path to a repo-relative POSIX path."""
        cleaned = normalize_path(path)
        absolute = Path(cleaned)
        if absolute.is_absolute():
            try:
                return normalize_path(absolute.resolve().relative_to(self.root.resolve()).as_posix())
            except (ValueError, OSError):
                return cleaned
        prefix = "" if self.project in (".", "") else f"{self.project}/"
        if prefix and not cleaned.startswith(prefix):
            return normalize_path(f"{prefix}{cleaned}")
        return cleaned

    def make_finding(self, **kwargs) -> Finding:
        kwargs.setdefault("project", self.project)
        return Finding(**kwargs)


class Adapter:
    """Protocol-like base class: adapters only produce normalized results."""

    name: str = ""
    lang: str = ""

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def failed(ctx: AdapterContext, tool: str, lang: str, reason: str) -> AnalyzerResult:
        return AnalyzerResult(
            tool=tool,
            lang=lang,
            project=ctx.project,
            coverage=Coverage.FAILED,
            coverage_reason=reason,
        )
