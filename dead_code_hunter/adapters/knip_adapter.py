"""knip adapter (local only) and the DP-5 completeness gate."""

from __future__ import annotations

from ..models import AnalyzerResult, Coverage
from ..parsers import KnipParseOutcome, parse_knip
from ..tools import find_local_knip, run, tool_version
from .base import Adapter, AdapterContext

KNIP_ARGS = ["--reporter", "json", "--no-progress"]


def _has_node_modules(ctx: AdapterContext) -> bool:
    """True when a node_modules tree is reachable inside the repository."""
    folder = ctx.project_root
    while True:
        if (folder / "node_modules").is_dir():
            return True
        if folder == ctx.root or folder.parent == folder:
            return False
        try:
            folder.relative_to(ctx.root)
        except ValueError:
            return False
        folder = folder.parent


class KnipAdapter(Adapter):
    name = "knip"
    lang = "js"

    def analyze(self, ctx: AdapterContext) -> AnalyzerResult:
        command = ctx.overrides.resolve(
            "knip", lambda: find_local_knip(ctx.project_root, ctx.root)
        )
        node_modules = _has_node_modules(ctx)
        if command is None:
            if not node_modules:
                return AnalyzerResult(
                    tool=self.name,
                    lang=self.lang,
                    project=ctx.project,
                    coverage=Coverage.PARTIAL,
                    coverage_reason=(
                        "node_modules is missing, so the dependency graph cannot be resolved "
                        "(JS-2); local knip is not installed either"
                    ),
                )
            return self.failed(
                ctx,
                self.name,
                self.lang,
                "knip is not installed locally; install it in the project (npm i -D knip). "
                "The scanner never downloads or runs npx.",
            )
        args = ctx.overrides.args_for("knip", KNIP_ARGS)
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
            base.coverage_reason = f"knip did not run: {result.error or result.status}"
            return base
        outcome: KnipParseOutcome = parse_knip(result.stdout)
        if not outcome.parsed:
            base.coverage_reason = (
                f"knip exit {result.exit_code} with unparseable output: " + "; ".join(outcome.errors)
            )
            base.diagnostics = {"stderr": result.stderr[:2000]}
            return base
        base.metadata = {
            "unresolved": outcome.unresolved,
            "configuration_hints": outcome.configuration_hints,
            "entry_files": outcome.entry_files,
            "has_entry_field": outcome.has_entry_field,
            "unknown_keys": sorted(set(outcome.unknown_keys)),
            "files": outcome.files,
        }
        base.diagnostics = {"warnings": outcome.warnings[:20], "stderr": result.stderr[:2000]}
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
        # Provisional: DP-5 (evaluate_dp5) may downgrade this to PARTIAL.
        if result.exit_code == 0:
            base.coverage = Coverage.COMPLETE
            base.coverage_reason = "knip exit 0 (no issues)"
        elif result.exit_code == 1:
            base.coverage = Coverage.COMPLETE
            base.coverage_reason = f"knip exit 1 (issues reported); {len(outcome.items)} item(s) parsed"
        else:
            base.coverage = Coverage.PARTIAL
            base.coverage_reason = (
                f"knip exit {result.exit_code}: {result.stderr.strip()[:300] or 'tool reported an error'}"
            )
        return base


def evaluate_dp5(
    result: AnalyzerResult,
    entrypoints: list,
    dynamic_imports: list[dict],
    packages_covered: bool,
    uncovered_packages: list[str],
) -> tuple[Coverage, str]:
    """DP-5: when is a knip scan COMPLETE?

    COMPLETE only when every condition below holds.  Each failing condition is
    named in the reason so the report never hides why it is partial.
    """
    if result.coverage is Coverage.FAILED:
        return Coverage.FAILED, result.coverage_reason or "knip failed"
    reasons: list[str] = []
    metadata = result.metadata or {}
    unresolved = metadata.get("unresolved") or []
    if unresolved:
        reasons.append(
            f"{len(unresolved)} unresolved import(s): "
            + ", ".join(sorted({str(u.get('name')) for u in unresolved})[:3])
        )
    if not packages_covered:
        reasons.append(
            "package.json not covered by exactly one run: " + ", ".join(uncovered_packages[:5])
        )
    hints = metadata.get("configuration_hints") or []
    if hints:
        reasons.append(
            f"{len(hints)} knip configuration hint(s) about missing plugins/entries"
        )
    if dynamic_imports:
        reasons.append(
            f"{len(dynamic_imports)} unresolved dynamic import(s): "
            + ", ".join(sorted({str(d.get('specifier')) for d in dynamic_imports})[:3])
        )
    resolved_entries = set(metadata.get("entry_files") or [])
    has_entry_field = bool(metadata.get("has_entry_field"))
    uncovered_entries = [
        entry.path
        for entry in entrypoints
        if not (has_entry_field and entry.path in resolved_entries)
    ]
    if uncovered_entries:
        reasons.append(
            "entrypoint(s) outside package.json not confirmed by knip: "
            + ", ".join(sorted(set(uncovered_entries))[:5])
        )
    if reasons:
        return Coverage.PARTIAL, "DP-5 incomplete: " + "; ".join(reasons)
    if result.coverage is Coverage.PARTIAL:
        # The adapter already found an analysis gap (missing node_modules, exit code...).
        return Coverage.PARTIAL, result.coverage_reason or "analysis incomplete"
    return Coverage.COMPLETE, result.coverage_reason or "DP-5 satisfied"
