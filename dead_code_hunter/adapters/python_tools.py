"""Python adapters: ruff (unused imports/variables) and vulture."""

from __future__ import annotations

import re

from ..models import AnalyzerResult, Coverage
from ..parsers import parse_ruff, parse_vulture
from ..tools import find_python_tool, run, tool_version
from .base import Adapter, AdapterContext

RUFF_ARGS = [
    "check",
    ".",
    "--select",
    "F401,F841",
    "--no-cache",
    "--output-format",
    "json",
]
VULTURE_ARGS = [
    ".",
    "--min-confidence",
    "60",
    "--exclude",
    ".venv,venv,build,dist,node_modules,.git,.dart_tool,__pycache__",
]


class RuffAdapter(Adapter):
    name = "ruff"
    lang = "python"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        command = ctx.overrides.resolve(
            "ruff", lambda: find_python_tool("ruff", ctx.project_root, ctx.root)
        )
        if command is None:
            return self.failed(ctx, self.name, self.lang, "ruff is not installed in the project .venv or on PATH")
        args = ctx.overrides.args_for("ruff", RUFF_ARGS)
        result = run(command, list(args), ctx.project_root, timeout=ctx.timeout)
        version = tool_version(command, ["--version"], ctx.project_root)
        base = AnalyzerResult(
            tool=self.name,
            lang=self.lang,
            project=ctx.project,
            coverage=Coverage.FAILED,
            tool_path=command.describe(),
            tool_version=version,
            command=command.argv(list(args)),
            exit_code=result.exit_code,
        )
        if not result.ok:
            base.coverage_reason = f"ruff did not run: {result.error or result.status}"
            return base
        outcome = parse_ruff(result.stdout)
        if outcome.errors:
            base.coverage_reason = (
                f"ruff exit {result.exit_code}: " + "; ".join(outcome.errors)
            )
            base.diagnostics = {"stderr": result.stderr[:2000]}
            return base
        if result.exit_code not in (0, 1):
            base.coverage_reason = (
                f"ruff exited with {result.exit_code}: {result.stderr.strip()[:300] or 'no diagnostics'}"
            )
            base.diagnostics = {"stderr": result.stderr[:2000]}
            return base
        base.coverage = Coverage.COMPLETE
        base.coverage_reason = f"ruff exit {result.exit_code}; {len(outcome.items)} finding(s) parsed"
        base.diagnostics = {"warnings": outcome.warnings[:20]}
        for item in outcome.items:
            base.findings.append(
                ctx.make_finding(
                    tool=self.name,
                    lang=self.lang,
                    kind=item.kind,
                    category=item.category,
                    file=ctx.rel(item.file),
                    line=item.line,
                    end_line=item.end_line,
                    symbol=item.symbol,
                    message=item.message,
                    signals=[item.signal],
                )
            )
        return base


class VultureAdapter(Adapter):
    name = "vulture"
    lang = "python"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        command = ctx.overrides.resolve(
            "vulture", lambda: find_python_tool("vulture", ctx.project_root, ctx.root)
        )
        if command is None:
            return self.failed(
                ctx,
                self.name,
                self.lang,
                "vulture is not installed in the project .venv or on PATH (install it locally, never automatically)",
            )
        args = ctx.overrides.args_for("vulture", VULTURE_ARGS)
        result = run(command, list(args), ctx.project_root, timeout=ctx.timeout)
        version = tool_version(command, ["--version"], ctx.project_root)
        base = AnalyzerResult(
            tool=self.name,
            lang=self.lang,
            project=ctx.project,
            coverage=Coverage.FAILED,
            tool_path=command.describe(),
            tool_version=version,
            command=command.argv(list(args)),
            exit_code=result.exit_code,
        )
        if not result.ok:
            base.coverage_reason = f"vulture did not run: {result.error or result.status}"
            return base
        outcome = parse_vulture(result.stdout)
        failed_files = _failed_files(result.stderr) + _failed_files(result.stdout)
        if result.exit_code not in (0, 1, 3):
            base.coverage = Coverage.FAILED
            base.coverage_reason = (
                f"vulture exit {result.exit_code}: {result.stderr.strip()[:300] or 'no diagnostics'}"
            )
            base.diagnostics = {"stderr": result.stderr[:2000]}
            return base
        if failed_files:
            # Vulture reports unparsable files on stderr but still exits 3 when
            # it also found something else, so the exit code alone cannot be
            # trusted: any named failure makes the run PARTIAL (spec PY-2,
            # BUG-03).  Verified against vulture 2.14 on this host.
            base.coverage = Coverage.PARTIAL
            base.coverage_reason = (
                f"vulture exit {result.exit_code} with unparsable file(s): "
                + ", ".join(sorted(set(failed_files))[:5])
            )
        elif result.exit_code == 1:
            base.coverage = Coverage.PARTIAL
            base.coverage_reason = "vulture exit 1 (analysis error; no file named in the output)"
        elif result.exit_code == 0:
            base.coverage = Coverage.COMPLETE
            base.coverage_reason = "vulture exit 0 (no unused code reported)"
        else:
            base.coverage = Coverage.COMPLETE
            base.coverage_reason = (
                f"vulture exit 3 (unused code reported); {len(outcome.items)} finding(s)"
            )
        base.diagnostics = {
            "warnings": outcome.warnings[:20],
            "failed_files": sorted(set(failed_files)),
            "stderr": result.stderr[:2000],
        }
        for item in outcome.items:
            base.findings.append(
                ctx.make_finding(
                    tool=self.name,
                    lang=self.lang,
                    kind=item.kind,
                    category=item.category,
                    file=ctx.rel(item.file),
                    line=item.line,
                    end_line=item.end_line,
                    symbol=item.symbol,
                    message=item.message,
                    signals=[item.signal],
                )
            )
        return base


_VULTURE_PARSE_ERROR = re.compile(
    r"^\s*(?P<path>[^\s:\"']+\.py):\d+:\s*(?:invalid syntax|error|"
    r"cannot (?:parse|compile)|SyntaxError)",
    re.MULTILINE | re.IGNORECASE,
)


def _failed_files(text: str) -> list[str]:
    """Files vulture could not analyse.

    Vulture names them as ``path.py:line: invalid syntax at "..."`` on stderr.
    Only diagnostic lines count: a normal finding line must never be mistaken
    for a failed file.
    """
    found: list[str] = []
    for pattern in (r'File "([^"]+\.py)"', r"(?P<path>[A-Za-z]:\\[^:\n\"']+\.py)(?=:\d+:\s*error)"):
        for match in re.finditer(pattern, text, re.MULTILINE):
            found.append(match.groupdict().get("path") or match.group(1))
    for match in _VULTURE_PARSE_ERROR.finditer(text):
        found.append(match.group("path"))
    return [value for value in found if value]
