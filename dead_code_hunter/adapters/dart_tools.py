"""Dart adapters: ``dart analyze`` and the scanner's own import graph."""

from __future__ import annotations

from ..analysis.dart_refs import DartFacts, orphan_files
from ..models import AnalyzerResult, Category, Coverage
from ..parsers import DART_INCOMPLETE_CODES, parse_dart
from ..tools import find_dart, run, tool_version
from .base import Adapter, AdapterContext

DART_ARGS = ["analyze", "--format=machine"]


class DartAnalyzeAdapter(Adapter):
    name = "dart analyze"
    lang = "dart"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        command = ctx.overrides.resolve("dart", find_dart)
        if command is None:
            return self.failed(ctx, self.name, self.lang, "dart SDK is not installed or not on PATH")
        args = ctx.overrides.args_for("dart", DART_ARGS)
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
            base.coverage_reason = f"dart analyze did not run: {result.error or result.status}"
            return base
        outcome = parse_dart(result.stdout)
        diagnostics = outcome.diagnostics
        base.diagnostics = {
            "errors": outcome.error_count,
            "warnings": outcome.warning_count,
            "infos": outcome.info_count,
            "stderr": result.stderr[:2000],
            "parse_warnings": outcome.warnings[:10],
        }
        if result.exit_code != 0 and not diagnostics:
            base.coverage = Coverage.FAILED
            detail = (result.stderr or result.stdout).strip()[:400]
            base.coverage_reason = (
                f"dart analyze exited {result.exit_code} without diagnostics"
                + (f": {detail}" if detail else "")
            )
            return base
        incomplete_codes = sorted(
            {d.code for d in diagnostics if d.code in DART_INCOMPLETE_CODES}
        )
        error_files = sorted({d.file for d in diagnostics if d.severity == "ERROR"})
        if incomplete_codes:
            base.coverage = Coverage.PARTIAL
            base.coverage_reason = (
                "unresolved references ("
                + ", ".join(incomplete_codes[:5])
                + ") in: "
                + ", ".join(sorted({d.file for d in diagnostics if d.code in DART_INCOMPLETE_CODES})[:5])
            )
        elif error_files:
            base.coverage = Coverage.PARTIAL
            base.coverage_reason = (
                "analysis of broken code is not reliable; compile error(s) in: "
                + ", ".join(error_files[:5])
            )
        else:
            base.coverage = Coverage.COMPLETE
            base.coverage_reason = (
                f"dart analyze exit {result.exit_code}; "
                f"{outcome.warning_count} warning(s), {outcome.info_count} info(s)"
            )
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


def graph_result(ctx: AdapterContext, facts: DartFacts) -> AnalyzerResult:
    """Orphan-file detection from the scanner's own import graph (DT-2)."""
    tool = "dart-import-graph"
    if not facts.files:
        return AnalyzerResult(
            tool=tool,
            lang="dart",
            project=ctx.project,
            coverage=Coverage.NOT_APPLICABLE,
            coverage_reason="no Dart files in scope",
        )
    result = AnalyzerResult(
        tool=tool,
        lang="dart",
        project=ctx.project,
        coverage=Coverage.COMPLETE,
        coverage_reason=(
            f"import graph over {len(facts.files)} Dart files; "
            f"{len(facts.entry_files)} entrypoint(s)"
        ),
    )
    if facts.unresolved:
        result.coverage = Coverage.PARTIAL
        result.coverage_reason = (
            "unresolved import URI(s): "
            + ", ".join(sorted({u["uri"] for u in facts.unresolved})[:5])
        )
    result.metadata = {
        "packages": [
            {"path": p.path, "name": p.name, "publish_to_none": p.publish_to_none}
            for p in facts.packages
        ],
        "entrypoints": sorted(facts.entry_files),
        "test_only_files": sorted(facts.test_only),
        "unresolved": facts.unresolved[:50],
    }
    for file in orphan_files(facts, ctx.project):
        result.findings.append(
            ctx.make_finding(
                tool=tool,
                lang="dart",
                kind="file",
                category=Category.DEAD_CODE,
                file=file,
                line=1,
                end_line=None,
                symbol=file,
                message="file is not reachable from any entrypoint in the import graph",
                signals=["dart:orphan-file"],
            )
        )
    return result
