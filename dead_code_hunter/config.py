"""Repository configuration readers: impact policy and ignore rules.

The scanner reads ``.dead-code/config.toml`` and ``.dead-code/ignore`` but never
writes them (spec report location).  Repository content is treated as data:
only known keys with primitive values are used, everything else is ignored and
reported.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Finding, Impact, normalize_path

DEFAULT_CRITICAL_TERMS: tuple[str, ...] = (
    "billing", "invoice", "vat", "fpa", "payment", "cash", "cod", "settlement",
    "auth", "oauth", "mydata", "aade", "timologio", "parastatiko", "ekkatharisi",
)
DEFAULT_HIGH_TERMS: tuple[str, ...] = (
    "tenant", "driver", "assignment", "shift", "notification", "push", "fcm",
    "job", "worker", "cron", "migration",
)

MAX_TIMEOUT = 900
MAX_PROJECTS = 500


@dataclass
class ImpactPolicy:
    critical: tuple[str, ...] = DEFAULT_CRITICAL_TERMS
    high: tuple[str, ...] = DEFAULT_HIGH_TERMS
    medium: tuple[str, ...] = ()

    def evaluate(self, file: str, symbol: str | None, project: str) -> tuple[Impact, list[str]]:
        haystack = [file.lower(), (symbol or "").lower(), project.lower()]
        reasons: list[str] = []
        critical_hits = self._hits(self.critical, haystack)
        if critical_hits:
            reasons.append(f"critical keyword(s) in path/name: {', '.join(critical_hits)}")
            return Impact.CRITICAL, reasons
        high_hits = self._hits(self.high, haystack)
        if high_hits:
            reasons.append(f"high-impact keyword(s) in path/name: {', '.join(high_hits)}")
            return Impact.HIGH, reasons
        medium_hits = self._hits(self.medium, haystack)
        if medium_hits:
            reasons.append(f"medium-impact keyword(s) in path/name: {', '.join(medium_hits)}")
            return Impact.MEDIUM, reasons
        return Impact.LOW, ["no high-impact keyword in path, name or project"]

    @staticmethod
    def _hits(terms: tuple[str, ...], haystack: list[str]) -> list[str]:
        hits: list[str] = []
        for term in terms:
            lowered = term.lower()
            if any(lowered in value for value in haystack):
                hits.append(term)
        return sorted(set(hits))

    def to_dict(self) -> dict[str, Any]:
        return {"critical": list(self.critical), "high": list(self.high), "medium": list(self.medium)}


@dataclass
class ScannerConfig:
    impact: ImpactPolicy = field(default_factory=ImpactPolicy)
    timeout_seconds: int = 180
    max_projects: int = 64
    unknown_keys: list[str] = field(default_factory=list)
    parse_error: str | None = None
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "impact": self.impact.to_dict(),
            "timeout_seconds": self.timeout_seconds,
            "max_projects": self.max_projects,
            "unknown_keys": self.unknown_keys,
            "parse_error": self.parse_error,
            "source": self.source,
        }


def _as_terms(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(v for v in value if v)
    return None


def load_config(root: Path) -> ScannerConfig:
    config = ScannerConfig()
    path = root / ".dead-code" / "config.toml"
    if not path.is_file():
        return config
    config.source = ".dead-code/config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        config.parse_error = f"{type(exc).__name__}: {exc}"
        return config

    impact = data.get("impact")
    if isinstance(impact, dict):
        critical = _as_terms(impact.get("critical"))
        high = _as_terms(impact.get("high"))
        medium = _as_terms(impact.get("medium"))
        config.impact = ImpactPolicy(
            critical=critical if critical is not None else DEFAULT_CRITICAL_TERMS,
            high=high if high is not None else DEFAULT_HIGH_TERMS,
            medium=medium if medium is not None else (),
        )
        for key in impact:
            if key not in {"critical", "high", "medium"}:
                config.unknown_keys.append(f"impact.{key}")

    scan = data.get("scan")
    if isinstance(scan, dict):
        timeout = scan.get("timeout_seconds")
        if isinstance(timeout, int) and not isinstance(timeout, bool):
            config.timeout_seconds = max(5, min(MAX_TIMEOUT, timeout))
        elif timeout is not None:
            config.unknown_keys.append("scan.timeout_seconds (invalid type)")
        max_projects = scan.get("max_projects")
        if isinstance(max_projects, int) and not isinstance(max_projects, bool):
            config.max_projects = max(1, min(MAX_PROJECTS, max_projects))
        elif max_projects is not None:
            config.unknown_keys.append("scan.max_projects (invalid type)")
        for key in scan:
            if key not in {"timeout_seconds", "max_projects"}:
                config.unknown_keys.append(f"scan.{key}")

    for key in data:
        if key not in {"impact", "scan"}:
            config.unknown_keys.append(key)
    return config


@dataclass
class IgnoreRule:
    raw: str
    pattern: str
    symbol: str | None = None

    def matches(self, finding: Finding) -> bool:
        path = normalize_path(finding.file)
        if not _glob_match(path, self.pattern):
            return False
        if self.symbol is None:
            return True
        return finding.symbol == self.symbol


def _glob_match(path: str, pattern: str) -> bool:
    pattern = normalize_path(pattern)
    if pattern.endswith("/"):
        pattern = pattern + "**"
    if fnmatch.fnmatch(path, pattern):
        return True
    # A bare directory name matches everything inside it.
    if "*" not in pattern and "?" not in pattern and "[" not in pattern:
        return path == pattern or path.startswith(f"{pattern}/")
    return False


@dataclass
class IgnoreList:
    rules: list[IgnoreRule] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    source: str | None = None

    def match(self, finding: Finding) -> str | None:
        for rule in self.rules:
            if rule.matches(finding):
                return rule.raw
        return None

    def is_empty(self) -> bool:
        return not self.rules


def load_ignore(root: Path) -> IgnoreList:
    """Parse ``.dead-code/ignore``.

    Line syntax::

        path/glob            # ignores every finding in matching files
        path/glob::symbol    # ignores one symbol in matching files
        # comment
    """
    result = IgnoreList()
    path = root / ".dead-code" / "ignore"
    if not path.is_file():
        return result
    result.source = ".dead-code/ignore"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        result.parse_errors.append(f"{type(exc).__name__}: {exc}")
        return result
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "::" in line:
            pattern, symbol = line.split("::", 1)
            pattern, symbol = pattern.strip(), symbol.strip()
            if not pattern or not symbol:
                result.parse_errors.append(f"line {number}: empty path or symbol")
                continue
            result.rules.append(IgnoreRule(raw=line, pattern=pattern, symbol=symbol))
        else:
            result.rules.append(IgnoreRule(raw=line, pattern=line))
    return result
