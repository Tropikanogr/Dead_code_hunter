"""Command line interface (the only entrypoint of M1)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import SCANNER_VERSION
from .models import LANGUAGES, Recommendation
from .report import default_reports_base, render_markdown, repo_id
from .scanner import ScanOptions, scan

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REPO_CHANGED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dead-code-hunter",
        description=(
            "Read-only dead code scanner with evidence-based classification. "
            "Writes reports outside the scanned repository and never modifies it."
        ),
    )
    parser.add_argument("--version", action="version", version=f"dead-code-hunter {SCANNER_VERSION}")
    sub = parser.add_subparsers(dest="command")

    scan_cmd = sub.add_parser("scan", help="scan a trusted local checkout")
    scan_cmd.add_argument("--repo", type=Path, default=Path("."), help="repository root (default: .)")
    scan_cmd.add_argument(
        "--lang",
        dest="langs",
        action="append",
        choices=list(LANGUAGES),
        help="restrict to a language (repeatable; default: all)",
    )
    scan_cmd.add_argument(
        "--scope",
        action="append",
        default=[],
        help="restrict to a repo-relative path (repeatable)",
    )
    scan_cmd.add_argument(
        "--format",
        choices=["summary", "json", "markdown", "none"],
        default="summary",
        help="stdout format (reports are always written to the report directory)",
    )
    scan_cmd.add_argument(
        "--print-report-path",
        action="store_true",
        help="print only the written report paths",
    )
    scan_cmd.add_argument(
        "--no-write",
        action="store_true",
        help="do not write report files (stdout only; useful for tests)",
    )
    scan_cmd.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="override the report base directory (must stay outside the repository)",
    )
    scan_cmd.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any analyzer coverage is FAILED",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "scan":
        parser.print_help()
        return EXIT_ERROR
    if args.repo is None:
        parser.error("--repo is required")

    options = ScanOptions(
        root=args.repo,
        langs=tuple(args.langs) if args.langs else LANGUAGES,
        scope=list(args.scope),
        write_reports=not args.no_write,
        reports_base=args.reports_dir,
    )
    try:
        outcome = scan(options)
    except NotADirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - last resort, never silent
        print(f"error: scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    report = outcome.report
    exit_code = outcome.exit_code
    if args.strict:
        gaps = strict_gaps(report)
        if gaps:
            exit_code = exit_code or EXIT_ERROR
            for gap in gaps[:10]:
                print(f"strict: {gap}", file=sys.stderr)

    if args.print_report_path:
        if outcome.json_path:
            print(outcome.json_path)
        if outcome.markdown_path:
            print(outcome.markdown_path)
    elif args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(report))
    elif args.format == "summary":
        _print_summary(report, outcome)
    if outcome.report_error:
        print(f"warning: {outcome.report_error}", file=sys.stderr)
    return exit_code


#: Coverage verdicts that mean "this analysis is not trustworthy enough".
STRICT_FAILING_COVERAGE = frozenset({"PARTIAL", "FAILED"})


def strict_gaps(report: dict) -> list[str]:
    """Everything that makes a scan incomplete (spec P1, `--strict`).

    ``--strict`` fails on PARTIAL as well as FAILED, on any project that was
    never analysed, on a scope that matched nothing, and on a truncated file
    index.  Absence of coverage rows counts as a failure too: a language with
    no rows has no evidence, which is not the same as "nothing found".
    """
    gaps: list[str] = []
    coverage = report.get("coverage") or []
    for entry in coverage:
        status = entry.get("coverage")
        if status in STRICT_FAILING_COVERAGE:
            gaps.append(
                f"{entry.get('project')} [{entry.get('lang')}] {entry.get('tool')}: "
                f"{status} - {entry.get('reason')}"
            )
        elif status not in {"COMPLETE", "NOT_APPLICABLE"}:
            gaps.append(
                f"{entry.get('project')} [{entry.get('lang')}] {entry.get('tool')}: "
                f"unknown coverage {status!r}"
            )
    if not coverage:
        gaps.append("no coverage recorded at all")

    projects = report.get("projects") or []
    langs = (report.get("scope") or {}).get("langs") or []
    covered = {(entry.get("project"), entry.get("lang")) for entry in coverage}
    for project in projects:
        for lang in project.get("langs") or []:
            if lang not in langs:
                continue
            if (project.get("path"), lang) not in covered:
                gaps.append(
                    f"{project.get('path')} [{lang}]: no coverage entry (never analysed)"
                )

    discovery = report.get("discovery") or {}
    if discovery.get("truncated"):
        gaps.append("file index truncated (max_files reached)")
    for skipped in discovery.get("skipped_projects") or []:
        gaps.append(f"project skipped: {skipped.get('project')} ({skipped.get('reason')})")

    scope = report.get("scope") or {}
    for term in scope.get("unmatched") or []:
        gaps.append(f"scope matched no files: {term}")
    for term in scope.get("outside_root") or []:
        gaps.append(f"scope outside the repository: {term}")
    return gaps


def _print_summary(report: dict, outcome) -> None:
    summary = report.get("summary", {})
    print(f"Dead Code Hunter {report.get('tool_version')} - scan {report.get('scan_id')}")
    print(f"repository: {report.get('repository', {}).get('path')}")
    print(f"projects: {len(report.get('projects', []))}  scope: {report.get('scope')}")
    print("")
    print("coverage:")
    for entry in report.get("coverage", []):
        print(
            f"  {entry.get('project')} [{entry.get('lang')}] {entry.get('tool')}: "
            f"{entry.get('coverage')} - {entry.get('reason')}"
        )
    print("")
    print("results:")
    by_recommendation = summary.get("by_recommendation", {})
    for recommendation in (
        Recommendation.PROTECTED.value,
        Recommendation.RETAIN.value,
        Recommendation.REVIEW_REQUIRED.value,
        Recommendation.CANDIDATE.value,
        Recommendation.INFO.value,
    ):
        print(f"  {recommendation}: {by_recommendation.get(recommendation, 0)}")
    print(f"  ignored: {summary.get('ignored_total', 0)}")
    candidates = [f for f in outcome.findings if f.recommendation is Recommendation.CANDIDATE]
    if candidates:
        print("")
        print("candidates (never proof of safety):")
        for finding in candidates:
            print(f"  {finding.id}: {finding.symbol or finding.file} in {finding.file}:{finding.line}")
    if outcome.json_path:
        print("")
        print(f"report: {outcome.json_path}")
    if outcome.markdown_path:
        print(f"markdown: {outcome.markdown_path}")
    git = report.get("git_integrity", {})
    if git.get("changed"):
        print("")
        print("WARNING: repository state changed during the scan: " + ", ".join(git.get("changed_files", [])))
    elif not git.get("verifiable"):
        print("")
        print(
            "NOTE: repository integrity could not be verified "
            f"(before={git.get('before', {}).get('status')}, after={git.get('after', {}).get('status')})."
        )


def describe_report_location(root: Path) -> str:
    return str(default_reports_base() / repo_id(root))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
