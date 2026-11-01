"""Scan orchestration: discovery → analyzers → evidence → classification → report.

The scanner never writes inside the scanned repository, never installs anything
and never hides a partial or failed analysis.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SCANNER_VERSION, gitstate
from .adapters.base import Adapter, AdapterContext
from .adapters.dart_tools import graph_result
from .adapters.knip_adapter import evaluate_dp5
from .adapters.registry import adapters_for
from .analysis import dart_refs, js_refs, python_refs
from .analysis.entrypoints import discover_entrypoints
from .classify import apply_classification
from .config import load_config, load_ignore
from .coverage import CoverageEntry, CoverageRegistry
from .dedupe import dedupe
from .discovery import Discovery, discover
from .models import (
    AnalyzerResult,
    Coverage,
    Finding,
    IgnoredFinding,
    LANGUAGES,
    normalize_path,
)
from .report import build_report, new_scan_id, report_dir, write_reports
from .tools import ToolOverrides


@dataclass
class ScanOptions:
    root: Path
    langs: tuple[str, ...] = LANGUAGES
    scope: list[str] = field(default_factory=list)
    overrides: ToolOverrides = field(default_factory=ToolOverrides)
    extra_adapters: list[type[Adapter]] = field(default_factory=list)
    write_reports: bool = True
    reports_base: Path | None = None
    scan_id: str | None = None


@dataclass
class ScanOutcome:
    scan_id: str
    root: Path
    report: dict[str, Any]
    findings: list[Finding]
    exit_code: int
    json_path: str | None = None
    markdown_path: str | None = None
    report_error: str | None = None


def _under(path: str, prefix: str) -> bool:
    if prefix in (".", ""):
        return True
    return path == prefix or path.startswith(f"{prefix}/")


def _in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    return any(_under(path, prefix) for prefix in scope)


def scan(options: ScanOptions) -> ScanOutcome:
    root = options.root.expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    scan_id = options.scan_id or new_scan_id()
    config = load_config(root)
    ignore_list = load_ignore(root)
    git_before = gitstate.capture(root)

    discovery = discover(root, max_projects=config.max_projects)
    files = list(discovery.all_files)
    scope = [normalize_path(p) for p in options.scope if p.strip()] if options.scope else []
    langs = tuple(lang for lang in options.langs if lang in LANGUAGES) or LANGUAGES

    projects = [p for p in discovery.projects if _in_scope(p.path, scope)] if scope else list(discovery.projects)
    if scope:
        files = [f for f in files if _in_scope(f, scope)]

    # ---- reference facts (whole repo: more references means more retention) ----
    python_facts = None
    if "python" in langs and any("python" in p.langs for p in projects):
        python_projects = [p.path for p in discovery.projects if "python" in p.langs]
        python_facts = python_refs.build_facts(root, files, python_projects)

    entrypoints, entrypoint_problems = discover_entrypoints(root, files)
    js_facts = None
    if "js" in langs and discovery.packages:
        js_facts = js_refs.build_facts(root, files, discovery.packages, entrypoints)

    dart_facts = None
    if "dart" in langs and any("dart" in p.langs for p in projects):
        dart_projects = [p.path for p in discovery.projects if "dart" in p.langs]
        dart_facts = dart_refs.build_facts(root, files, dart_projects)

    # ---- analyzer runs -------------------------------------------------------
    results: list[AnalyzerResult] = []
    js_run_projects = _js_run_projects(discovery)
    for project in projects:
        for lang in project.langs:
            if lang not in langs:
                continue
            if lang == "js" and project.path not in js_run_projects:
                continue  # covered by the monorepo run
            ctx = AdapterContext(
                root=root,
                project=project.path,
                repo_files=files,
                config=config,
                overrides=options.overrides,
            )
            for adapter_cls in adapters_for(lang, options.extra_adapters):
                results.append(_run_adapter(adapter_cls, ctx))
            if lang == "dart" and dart_facts is not None:
                results.append(graph_result(ctx, dart_facts))

    # DP-5: completeness of knip is a policy decision, not a parsing one.
    if js_facts is not None:
        covered, uncovered = _package_coverage(discovery, js_run_projects)
        for result in results:
            if result.tool != "knip":
                continue
            run_prefix = result.project
            scoped_entrypoints = [e for e in js_facts.entrypoints if _under(e.path, run_prefix)]
            scoped_dynamic = [
                d for d in js_facts.unresolved_dynamic if _under(str(d.get("file", "")), run_prefix)
            ]
            coverage, reason = evaluate_dp5(
                result,
                scoped_entrypoints,
                scoped_dynamic,
                packages_covered=covered,
                uncovered_packages=uncovered,
            )
            result.coverage = coverage
            result.coverage_reason = reason

    # A Dart orphan file is only as trustworthy as the whole Dart analysis: if
    # `dart analyze` failed, the import graph cannot claim COMPLETE (DT-1).
    _harmonize_language_coverage(results, "dart")

    # ---- coverage registry (no silent gaps) ---------------------------------
    registry = CoverageRegistry()
    for result in results:
        registry.add_result(result)
    _fill_inherited_coverage(registry, projects, results, langs)

    # ---- enrichment ----------------------------------------------------------
    dirty_paths = set(git_before.dirty_files()) if git_before.status == "ok" else set()
    findings: list[Finding] = []
    for result in results:
        for finding in result.findings:
            owner = discovery.owner(finding.file)
            finding.project = owner
            if dart_facts is not None and result.lang == "dart" and finding.file in dart_facts.generated:
                continue  # generated Dart sources are excluded by DT-3
            impact, reasons = config.impact.evaluate(finding.file, finding.symbol, owner)
            finding.impact = impact
            finding.impact_reasons = reasons
            finding.coverage = result.coverage
            _enrich(finding, result, python_facts, js_facts, dart_facts, result.coverage)
            if not finding.coverage_reasons:
                finding.coverage_reasons.append(
                    f"{result.tool} coverage {finding.coverage.value}"
                )
            if finding.file in dirty_paths:
                # X-5: informational only; it never changes the recommendation.
                finding.signals = sorted(set(finding.signals) | {"uncommitted-changes"})
            finding.scan_id = scan_id
            finding.fingerprint = finding.compute_fingerprint()
            finding.id = finding.compute_id()
            findings.append(finding)

    merged, dedupe_removed = dedupe(findings)

    kept: list[Finding] = []
    ignored: list[IgnoredFinding] = []
    for finding in merged:
        apply_classification(finding)
        rule = ignore_list.match(finding)
        if rule is not None:
            finding.scan_id = scan_id
            ignored.append(IgnoredFinding(finding=finding, rule=rule))
        else:
            kept.append(finding)
    _sort_findings(kept)

    # ---- report --------------------------------------------------------------
    git_after = gitstate.capture(root)
    integrity = gitstate.GitIntegrity(before=git_before, after=git_after).evaluate()
    isolation = {
        "os_sandbox": False,
        "network_blocked": False,
        "snapshot": False,
        "secrets_excluded": True,
        "env_scrubbed": True,
        "note": (
            "M1 runs analyzers on the real checkout: no snapshot, no OS sandbox and no network "
            "block. Only secrets exclusion and environment scrubbing are enforced. "
            "Scan only repositories you trust."
        ),
    }
    tools = _tool_table(results)
    entrypoint_rows = _entrypoint_rows(js_facts, dart_facts)
    discovery_meta = {
        "skipped_secret_files": getattr(discovery, "meta", {}).get("skipped_secret_files", 0),
        "skipped_links": getattr(discovery, "meta", {}).get("skipped_links", 0),
        "truncated": discovery.truncated,
        "project_limit_reached": getattr(discovery, "project_limit_reached", False),
        "skipped_projects": discovery.skipped,
        "entrypoint_problems": entrypoint_problems,
        "monorepo_root": discovery.monorepo_root,
    }
    report = build_report(
        scan_id=scan_id,
        scanner_version=SCANNER_VERSION,
        root=root,
        langs=list(langs),
        scope_paths=scope,
        projects=[p.to_dict() for p in discovery.projects],
        coverage=registry.to_list(),
        findings=kept,
        ignored=ignored,
        isolation=isolation,
        git={
            **integrity.to_dict(),
            "revision": {
                "head": git_after.head or git_before.head,
                "dirty": integrity.after.dirty,
                "dirty_files": integrity.after.dirty_files(),
            },
        },
        tools=tools,
        entrypoints=entrypoint_rows,
        config={
            "scanner": config.to_dict(),
            "ignore": {
                "source": ignore_list.source,
                "rules": [r.raw for r in ignore_list.rules],
                "parse_errors": ignore_list.parse_errors,
            },
        },
        discovery_meta=discovery_meta,
        dedupe_removed=dedupe_removed,
    )

    json_path = markdown_path = None
    report_error = None
    if options.write_reports:
        try:
            target = report_dir(root, scan_id, options.reports_base)
            bundle = write_reports(report, target)
            report_error = bundle.error
            json_path = str(bundle.json_path) if bundle.json_path else None
            markdown_path = str(bundle.markdown_path) if bundle.markdown_path else None
        except (OSError, ValueError) as exc:
            report_error = str(exc)

    exit_code = 0
    if integrity.changed:
        exit_code = 2
    return ScanOutcome(
        scan_id=scan_id,
        root=root,
        report=report,
        findings=kept,
        exit_code=exit_code,
        json_path=json_path,
        markdown_path=markdown_path,
        report_error=report_error,
    )


def _js_run_projects(discovery: Discovery) -> list[str]:
    if not discovery.packages:
        return []
    if discovery.monorepo_root is not None:
        return [discovery.monorepo_root]
    return sorted({pkg.dir for pkg in discovery.packages})


def _package_coverage(discovery: Discovery, runs: list[str]) -> tuple[bool, list[str]]:
    uncovered: list[str] = []
    for pkg in discovery.packages:
        hits = [run for run in runs if _under(pkg.dir, run)]
        if len(hits) != 1:
            uncovered.append(pkg.path + f" ({len(hits)} run(s))")
    return not uncovered, uncovered


def _run_adapter(adapter_cls: type[Adapter], ctx: AdapterContext) -> AnalyzerResult:
    adapter = adapter_cls()
    try:
        return adapter.analyze(ctx)
    except Exception as exc:  # never let a broken analyzer look successful
        return AnalyzerResult(
            tool=adapter.name or adapter_cls.__name__,
            lang=adapter.lang,
            project=ctx.project,
            coverage=Coverage.FAILED,
            coverage_reason=f"adapter raised {type(exc).__name__}: {exc}",
            diagnostics={"traceback": traceback.format_exc()[-2000:]},
        )


def _enrich(
    finding: Finding,
    result: AnalyzerResult,
    python_facts,
    js_facts,
    dart_facts,
    coverage: Coverage,
) -> None:
    if result.lang == "python" and python_facts is not None:
        python_refs.enrich(finding, python_facts, coverage, finding.project)
    elif result.lang == "js" and js_facts is not None:
        js_refs.enrich(finding, js_facts, coverage)
    elif result.lang == "dart" and dart_facts is not None:
        dart_refs.enrich(finding, dart_facts, coverage, finding.project)


def _harmonize_language_coverage(results: list[AnalyzerResult], lang: str) -> None:
    """A language finding inherits the worst coverage of that language's tools."""
    from .coverage import worst

    groups: dict[str, list[AnalyzerResult]] = {}
    for result in results:
        if result.lang == lang:
            groups.setdefault(result.project, []).append(result)
    for group in groups.values():
        status = worst(*(r.coverage for r in group))
        if all(r.coverage is status for r in group):
            continue
        for result in group:
            if result.coverage is not status:
                detail = ", ".join(f"{r.tool}={r.coverage.value}" for r in group)
                result.coverage = status
                result.coverage_reason = (
                    f"{result.coverage_reason or ''}; language coverage limited by: {detail}"
                ).strip("; ")


def _fill_inherited_coverage(
    registry: CoverageRegistry, projects, results: list[AnalyzerResult], langs: tuple[str, ...]
) -> None:
    for project in projects:
        for lang in project.langs:
            if lang not in langs:
                continue
            if lang not in {"python", "js", "dart"}:
                continue
            relevant = [r for r in results if r.lang == lang and _under(project.path, r.project)]
            if not relevant:
                registry.add(
                    CoverageEntry(
                        project=project.path,
                        lang=lang,
                        tool="<none>",
                        coverage=Coverage.FAILED,
                        reason="no analyzer ran for this project/language",
                    )
                )
                continue
            if any(r.project == project.path for r in relevant):
                continue
            ancestor = max(relevant, key=lambda r: len(r.project))
            registry.add(
                CoverageEntry(
                    project=project.path,
                    lang=lang,
                    tool=ancestor.tool,
                    coverage=ancestor.coverage,
                    reason=f"covered by the run at '{ancestor.project}': {ancestor.coverage_reason}",
                    tool_path=ancestor.tool_path,
                    tool_version=ancestor.tool_version,
                    exit_code=ancestor.exit_code,
                    command=ancestor.command,
                    inherited=True,
                )
            )


def _tool_table(results: list[AnalyzerResult]) -> list[dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for result in results:
        existing = table.get(result.tool)
        if existing is None or (result.tool_path and not existing.get("path")):
            table[result.tool] = {
                "name": result.tool,
                "path": result.tool_path,
                "version": result.tool_version,
                "missing_reason": None if result.coverage is not Coverage.FAILED else result.coverage_reason,
            }
    return [table[name] for name in sorted(table)]


def _entrypoint_rows(js_facts, dart_facts) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if js_facts is not None:
        for entry in js_facts.entrypoints:
            rows.append(
                {
                    "path": entry.path,
                    "declared_by": entry.source,
                    "kind": entry.kind,
                    "lang": "js",
                    "protected": True,
                    "coverage": "declared",
                }
            )
    if dart_facts is not None:
        for entry in sorted(dart_facts.entry_files):
            rows.append(
                {
                    "path": entry,
                    "declared_by": "dart entrypoint",
                    "kind": "dart-main",
                    "lang": "dart",
                    "protected": True,
                    "coverage": "declared",
                }
            )
    rows.sort(key=lambda row: (row["lang"], row["path"], row["declared_by"]))
    return rows


def _sort_findings(findings: list[Finding]) -> None:
    findings.sort(
        key=lambda f: (
            {"PROTECTED": 0, "RETAIN": 1, "REVIEW_REQUIRED": 2, "CANDIDATE": 3, "INFO": 4}.get(
                f.recommendation.value if f.recommendation else "", 9
            ),
            f.file,
            f.line if f.line is not None else -1,
            f.symbol or "",
        )
    )
