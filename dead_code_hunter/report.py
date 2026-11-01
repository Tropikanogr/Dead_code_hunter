"""Report generation.  Reports are always written outside the scanned repo."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Finding, IgnoredFinding
from .safety import is_within

REPORT_ROOT_ENV = "DEAD_CODE_HUNTER_REPORTS"


def new_scan_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(3)


def repo_id(root: Path) -> str:
    resolved = root.resolve()
    digest = hashlib.sha1(str(resolved).lower().encode("utf-8")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "repo"
    return f"{name.lower()}-{digest}"


def default_reports_base() -> Path:
    override = os.environ.get(REPORT_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "dead-code-hunter"
    return Path.home() / ".local" / "share" / "dead-code-hunter"


def report_dir(root: Path, scan_id: str, base: Path | None = None) -> Path:
    base = base or default_reports_base()
    target = base / repo_id(root) / scan_id
    if is_within(target, root):
        raise ValueError(
            "refusing to write the report inside the scanned repository; "
            f"configured location {target} resolves inside {root}"
        )
    return target


@dataclass
class ReportBundle:
    data: dict[str, Any]
    json_path: Path | None = None
    markdown_path: Path | None = None
    error: str | None = None


def build_report(
    *,
    scan_id: str,
    scanner_version: str,
    root: Path,
    langs: list[str],
    scope_paths: list[str],
    scope_detail: dict[str, Any] | None = None,
    projects: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    findings: list[Finding],
    ignored: list[IgnoredFinding],
    isolation: dict[str, Any],
    git: dict[str, Any],
    tools: list[dict[str, Any]],
    entrypoints: list[dict[str, Any]],
    config: dict[str, Any],
    discovery_meta: dict[str, Any],
    dedupe_removed: int,
) -> dict[str, Any]:
    summary = summarize(findings, coverage, ignored)
    return {
        "scan_id": scan_id,
        "tool_version": scanner_version,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository": {"path": str(root), "repo_id": repo_id(root)},
        "revision": git.get("revision", {}),
        "scope": {
            "paths": scope_paths or ["."],
            "langs": langs,
            **(scope_detail or {}),
        },
        "projects": projects,
        "coverage": coverage,
        "isolation": isolation,
        "ignored": [entry.to_dict() for entry in ignored],
        "summary": summary,
        "findings": [f.to_dict() for f in findings],
        "entrypoints": entrypoints,
        "tools": tools,
        "config": config,
        "git_integrity": git,
        "discovery": discovery_meta,
        "deduplicated": dedupe_removed,
        "note": (
            "Candidate detection only. A CANDIDATE is a strong candidate for review, "
            "never proof that code is safe to delete."
        ),
    }


def summarize(
    findings: list[Finding], coverage: list[dict[str, Any]], ignored: list[IgnoredFinding]
) -> dict[str, Any]:
    by_recommendation: dict[str, int] = {}
    by_lang: dict[str, dict[str, int]] = {}
    for finding in findings:
        key = finding.recommendation.value if finding.recommendation else "UNKNOWN"
        by_recommendation[key] = by_recommendation.get(key, 0) + 1
        lang_bucket = by_lang.setdefault(finding.lang, {})
        lang_bucket[key] = lang_bucket.get(key, 0) + 1
    coverage_by_status: dict[str, int] = {}
    for entry in coverage:
        status = str(entry.get("coverage"))
        coverage_by_status[status] = coverage_by_status.get(status, 0) + 1
    return {
        "findings_total": len(findings),
        "by_recommendation": dict(sorted(by_recommendation.items())),
        "by_lang": {k: dict(sorted(v.items())) for k, v in sorted(by_lang.items())},
        "coverage_by_status": dict(sorted(coverage_by_status.items())),
        "ignored_total": len(ignored),
        "candidate_never_safe": True,
    }


def write_reports(bundle_data: dict[str, Any], target_dir: Path) -> ReportBundle:
    bundle = ReportBundle(data=bundle_data)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / "report.json"
        json_path.write_text(
            json.dumps(bundle_data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        markdown_path = target_dir / "report.md"
        markdown_path.write_text(render_markdown(bundle_data), encoding="utf-8")
        bundle.json_path = json_path
        bundle.markdown_path = markdown_path
    except OSError as exc:
        bundle.error = f"could not write report: {exc}"
    return bundle


RECOMMENDATION_TITLES = {
    "PROTECTED": "Protected (public contract)",
    "RETAIN": "Retain (active reference found)",
    "REVIEW_REQUIRED": "Review required",
    "CANDIDATE": "Candidates (strong, still review before deletion)",
    "INFO": "Info (not dead code)",
}


def render_markdown(data: dict[str, Any]) -> str:
    summary = data.get("summary", {})
    lines: list[str] = []
    lines.append(f"# Dead Code Hunter report — `{data.get('scan_id')}`")
    lines.append("")
    lines.append(f"Repository: `{data.get('repository', {}).get('path')}`")
    lines.append(f"Scanner version: {data.get('tool_version')}")
    lines.append(f"Generated: {data.get('generated_at')}")
    revision = data.get("revision", {})
    lines.append(
        f"Revision: `{revision.get('head') or 'unknown'}` (dirty: {revision.get('dirty')})"
    )
    scope = data.get("scope", {})
    if scope.get("requested"):
        lines.append("")
        lines.append(f"Scope: {', '.join(f'`{item}`' for item in scope['requested'])}")
        if scope.get("unmatched"):
            lines.append(
                "> Unmatched scope: "
                + ", ".join(f"`{item}`" for item in scope["unmatched"])
                + " — the scan is incomplete, never clean."
            )
        if scope.get("outside_root"):
            lines.append(
                "> Rejected scope outside the repository: "
                + ", ".join(f"`{item}`" for item in scope["outside_root"])
            )
    lines.append("")
    lines.append(
        "> A CANDIDATE is a strong candidate for human review. It is never proof that code is safe to delete."
    )
    lines.append("")

    isolation = data.get("isolation", {})
    lines.append("## Isolation")
    lines.append("")
    lines.append("| Setting | Value |")
    lines.append("| --- | --- |")
    for key in (
        "os_sandbox",
        "network_blocked",
        "snapshot",
        "secrets_excluded",
        "env_scrubbed",
    ):
        lines.append(f"| {key} | {isolation.get(key)} |")
    lines.append("")

    lines.append("## Coverage")
    lines.append("")
    lines.append("| Project | Language | Tool | Coverage | Reason |")
    lines.append("| --- | --- | --- | --- | --- |")
    for entry in data.get("coverage", []):
        reason = str(entry.get("reason") or "").replace("|", "/")
        lines.append(
            f"| `{entry.get('project')}` | {entry.get('lang')} | {entry.get('tool')} | "
            f"**{entry.get('coverage')}** | {reason} |"
        )
    if not data.get("coverage"):
        lines.append("| — | — | — | — | no analyzer ran |")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Recommendation | Count |")
    lines.append("| --- | --- |")
    for key, value in summary.get("by_recommendation", {}).items():
        lines.append(f"| {key} | {value} |")
    lines.append(f"| ignored | {summary.get('ignored_total', 0)} |")
    lines.append("")

    tools = data.get("tools", [])
    if tools:
        lines.append("## Tools")
        lines.append("")
        lines.append("| Tool | Path | Version |")
        lines.append("| --- | --- | --- |")
        for tool in tools:
            lines.append(
                f"| {tool.get('name')} | `{tool.get('path') or 'missing'}` | "
                f"{tool.get('version') or 'unknown'} |"
            )
        lines.append("")

    entrypoints = data.get("entrypoints", [])
    if entrypoints:
        lines.append("## Entrypoints used for protection")
        lines.append("")
        lines.append("| Entrypoint | Declared by | Protected |")
        lines.append("| --- | --- | --- |")
        for entry in entrypoints:
            lines.append(
                f"| `{entry.get('path')}` | {entry.get('declared_by')} | {entry.get('protected')} |"
            )
        lines.append("")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in data.get("findings", []):
        grouped.setdefault(str(finding.get("recommendation")), []).append(finding)
    for recommendation in (
        "PROTECTED",
        "RETAIN",
        "REVIEW_REQUIRED",
        "CANDIDATE",
        "INFO",
    ):
        items = grouped.get(recommendation)
        if not items:
            continue
        lines.append(f"## {RECOMMENDATION_TITLES.get(recommendation, recommendation)} ({len(items)})")
        lines.append("")
        for finding in items:
            lines.append(
                f"- `{finding.get('id')}` **{finding.get('symbol') or finding.get('file')}** "
                f"({finding.get('kind')}) — {finding.get('file')}:{finding.get('line')} "
                f"— rule {finding.get('rule')} — {finding.get('rule_reason')}"
            )
        lines.append("")

    ignored = data.get("ignored", [])
    if ignored:
        lines.append(f"## Ignored ({len(ignored)})")
        lines.append("")
        for entry in ignored:
            finding = entry.get("finding", {})
            lines.append(
                f"- rule `{entry.get('rule')}` → `{finding.get('symbol') or finding.get('file')}` "
                f"in `{finding.get('file')}`"
            )
        lines.append("")

    git = data.get("git_integrity", {})
    lines.append("## Repository integrity")
    lines.append("")
    lines.append(
        f"- git status before: {git.get('before', {}).get('status')}"
        f" ({len(git.get('before', {}).get('dirty_files', []))} dirty file(s))"
    )
    lines.append(
        f"- git status after: {git.get('after', {}).get('status')}"
        f" ({len(git.get('after', {}).get('dirty_files', []))} dirty file(s))"
    )
    lines.append(f"- changed during scan: {git.get('changed')}")
    discovery = data.get("discovery", {})
    lines.append(f"- skipped secret files: {discovery.get('skipped_secret_files')}")
    lines.append(f"- skipped symlinks: {discovery.get('skipped_links')}")
    lines.append(f"- discovery truncated: {discovery.get('truncated')}")
    for skipped in discovery.get("skipped_projects", []):
        lines.append(f"- skipped project `{skipped.get('project')}`: {skipped.get('reason')}")
    lines.append("")
    return "\n".join(lines)



