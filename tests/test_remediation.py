"""Regression tests for the M1 review remediation.

Every test in this module encodes a behaviour that the reviewed baseline
(commit 172dfe6) got wrong.  They are written before the fixes so the
FAIL-before / PASS-after evidence is real and reproducible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dead_code_hunter.models import (
    Confidence,
    Contract,
    Coverage,
    Finding,
    Recommendation,
    ReferenceKind,
)
from dead_code_hunter.scanner import ScanOptions, scan
from dead_code_hunter.tools import ToolOverrides

from .conftest import stub_command

PYPROJECT = '[project]\nname = "{name}"\nversion = "0.1.0"\n'


def write_repo(base: Path, files: dict[str, str]) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return repo


def tool_overrides(base: Path, **tools: dict) -> ToolOverrides:
    overrides = ToolOverrides()
    for name, config in tools.items():
        overrides.commands[name] = stub_command(base, name, config)
    return overrides


def vulture(*entries: tuple[str, int, str, str]) -> dict:
    """Build a vulture stub run from (file, line, kind, name) tuples."""
    lines = [
        f"{path}:{line}: unused {kind} '{name}' (60% confidence)"
        for path, line, kind, name in entries
    ]
    return {"exit": 3 if lines else 0, "stdout": "\n".join(lines) + ("\n" if lines else "")}


def run_scan(
    repo: Path,
    base: Path,
    *,
    overrides: ToolOverrides | None = None,
    langs: tuple[str, ...] = ("python", "js", "dart"),
    scope: list[str] | None = None,
    scan_id: str = "remediation-scan",
):
    return scan(
        ScanOptions(
            root=repo,
            langs=langs,
            scope=scope or [],
            overrides=overrides or ToolOverrides(),
            write_reports=False,
            scan_id=scan_id,
        )
    )


def coverage_entries(outcome) -> list[dict]:
    return outcome.report["coverage"]


def coverage_for(outcome, tool: str, project: str | None = None) -> dict:
    for entry in coverage_entries(outcome):
        if entry["tool"] == tool and (project is None or entry["project"] == project):
            return entry
    raise AssertionError(f"no coverage entry for {tool}/{project}: {coverage_entries(outcome)}")


# --------------------------------------------------------------------------- #
# CRITICAL: scope must limit reported findings, never the reference index
# --------------------------------------------------------------------------- #


def test_scope_keeps_reference_evidence_from_outside_the_scope(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="scope-app"),
            "app/__init__.py": "",
            "app/core.py": "def helper_value():\n    return 1\n",
            "app/use.py": "from app.core import helper_value\n\nprint(helper_value())\n",
            "app/other.py": "def orphan_in_other():\n    return 2\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture=vulture(
            ("app/core.py", 1, "function", "helper_value"),
            ("app/other.py", 1, "function", "orphan_in_other"),
        ),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",), scope=["app/core.py"])

    symbols = {f.symbol: f for f in out.findings}
    assert "helper_value" in symbols, "the finding inside the scope must be reported"
    assert "orphan_in_other" not in symbols, "findings outside the scope must not be reported"
    retained = symbols["helper_value"]
    assert retained.reference is ReferenceKind.ACTIVE, retained.reference_reasons
    assert retained.recommendation is Recommendation.RETAIN


def test_scope_inside_a_project_enables_the_parent_project_analysis(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="scope-parent"),
            "app/__init__.py": "",
            "app/core.py": "def scoped_dead():\n    return 1\n",
            "app/other.py": "def outside_scope():\n    return 2\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture=vulture(
            ("app/core.py", 1, "function", "scoped_dead"),
            ("app/other.py", 1, "function", "outside_scope"),
        ),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",), scope=["app/core.py"])

    executed = [e for e in coverage_entries(out) if e["lang"] == "python" and not e["inherited"]]
    assert executed, "the parent project must run its analyzers for a nested scope"
    assert all(entry["tool"] != "<none>" for entry in executed)
    assert all(entry["coverage"] != "FAILED" for entry in executed)
    assert {f.symbol for f in out.findings} == {"scoped_dead"}


def test_scope_that_matches_no_files_is_an_explicit_coverage_failure(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="scope-empty"),
            "app/__init__.py": "",
            "app/core.py": "def real_function():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
    )

    out = run_scan(
        repo, tmp_path, overrides=overrides, langs=("python",), scope=["does/not/exist.py"]
    )

    entries = coverage_entries(out)
    scope_entries = [entry for entry in entries if "scope" in (entry["reason"] or "").lower()]
    assert scope_entries, f"an unmatched scope must be reported, got {entries}"
    assert any(entry["coverage"] == Coverage.FAILED.value for entry in scope_entries)
    assert out.report["scope"]["unmatched"], "the report must name the unmatched scope terms"


def test_scope_outside_the_repository_is_an_explicit_failure(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="scope-escape"),
            "app/__init__.py": "",
            "app/core.py": "def real_function():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",), scope=["../outside"])

    entries = coverage_entries(out)
    assert any(
        entry["coverage"] == Coverage.FAILED.value and "scope" in (entry["reason"] or "").lower()
        for entry in entries
    ), entries


# --------------------------------------------------------------------------- #
# CRITICAL: coverage integrity
# --------------------------------------------------------------------------- #


def test_truncated_file_index_downgrades_coverage_to_partial(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="trunc"),
            ".dead-code/config.toml": "[scan]\nmax_files = 2\n",
            "app/__init__.py": "",
            "app/a.py": "def first_dead():\n    return 1\n",
            "app/b.py": "def second_dead():\n    return 2\n",
            "app/c.py": "def third_dead():\n    return 3\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture=vulture(("app/a.py", 1, "function", "first_dead")),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    assert out.report["discovery"]["truncated"] is True
    for finding in out.findings:
        assert finding.coverage is Coverage.PARTIAL, (
            f"{finding.symbol} kept {finding.coverage} despite a truncated file index"
        )
        assert finding.recommendation is not Recommendation.CANDIDATE


def test_project_limit_creates_explicit_failed_coverage_entries(tmp_path):
    files = {"pyproject.toml": PYPROJECT.format(name="limit-root")}
    for index in range(3):
        files[f"svc{index}/pyproject.toml"] = PYPROJECT.format(name=f"svc{index}")
        files[f"svc{index}/app.py"] = f"def svc{index}_dead():\n    return {index}\n"
    files[".dead-code/config.toml"] = "[scan]\nmax_projects = 1\n"
    repo = write_repo(tmp_path, files)
    overrides = tool_overrides(
        tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    skipped = out.report["discovery"]["skipped_projects"]
    assert skipped, "the report must list projects dropped by the limit"
    failed = [
        entry
        for entry in coverage_entries(out)
        if entry["coverage"] == Coverage.FAILED.value and entry["tool"] == "<skipped>"
    ]
    assert {entry["project"] for entry in failed} == {item["project"] for item in skipped}


def test_missing_discovery_evidence_never_yields_a_candidate(tmp_path):
    """A truncated index cannot prove absence, so R8 must not fire."""
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="trunc-candidate"),
            ".dead-code/config.toml": "[scan]\nmax_files = 3\n",
            "app/__init__.py": "",
            "app/core.py": "def maybe_used():\n    return 1\n",
            "app/z1.py": "x = 1\n",
            "app/z2.py": "y = 2\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture=vulture(("app/core.py", 1, "function", "maybe_used")),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    finding = next(f for f in out.findings if f.symbol == "maybe_used")
    assert finding.recommendation is Recommendation.REVIEW_REQUIRED
    assert finding.rule.value == "R4"


def test_strict_exit_code_covers_partial_coverage(tmp_path):
    """--strict must fail on PARTIAL, not only on FAILED."""
    from dead_code_hunter import cli

    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="strict-partial"),
            "app/__init__.py": "",
            "app/core.py": "def helper():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture={"exit": 1, "stderr": "app/core.py:1: error: invalid syntax\n", "stdout": ""},
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))
    assert coverage_for(out, "vulture")["coverage"] == Coverage.PARTIAL.value

    import dead_code_hunter.scanner as scanner_module

    original = scanner_module.ScanOptions

    def patched(**kwargs):
        kwargs.setdefault("overrides", overrides)
        kwargs.setdefault("write_reports", False)
        return original(**kwargs)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "ScanOptions", patched, raising=True)
    try:
        code = cli.main(["scan", "--strict", "--repo", str(repo), "--no-write", "--format", "none"])
    finally:
        monkey.undo()
    assert code != 0, "PARTIAL coverage must fail the strict gate"


def test_dedupe_never_upgrades_coverage_to_complete():
    from dead_code_hunter.dedupe import dedupe

    complete = Finding(
        tool="ruff",
        lang="python",
        project=".",
        kind="import",
        category=__import__("dead_code_hunter.models", fromlist=["Category"]).Category.DEAD_CODE,
        file="app/utils.py",
        line=1,
        symbol="json",
        confidence=Confidence.HIGH,
        coverage=Coverage.COMPLETE,
        reference=ReferenceKind.NONE,
    )
    partial = Finding(
        tool="vulture",
        lang="python",
        project=".",
        kind="import",
        category=__import__("dead_code_hunter.models", fromlist=["Category"]).Category.DEAD_CODE,
        file="app/utils.py",
        line=1,
        symbol="json",
        confidence=Confidence.MEDIUM,
        coverage=Coverage.PARTIAL,
        reference=ReferenceKind.NONE,
    )

    merged, _ = dedupe([complete, partial])
    assert len(merged) == 1
    assert merged[0].coverage is Coverage.PARTIAL, (
        "merging must keep the weakest coverage; a COMPLETE tool cannot vouch for a partial one"
    )


# --------------------------------------------------------------------------- #
# PYTHON: discovery and contract classification
# --------------------------------------------------------------------------- #


def test_python_project_with_sources_only_in_subfolders_is_discovered(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "backend/pyproject.toml": PYPROJECT.format(name="backend"),
            "backend/app/main.py": "def backend_dead():\n    return 1\n",
        },
    )
    out = run_scan(repo, tmp_path, langs=("python",))

    projects = {project["path"] for project in out.report["projects"]}
    assert "backend" in projects, f"marker project with nested sources missing: {projects}"
    assert all(
        entry["project"] == "backend"
        for entry in coverage_entries(out)
        if entry["lang"] == "python"
    )


def test_src_layout_package_is_analysed_as_a_project_package(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="mypkg"),
            "src/mypkg/__init__.py": "from mypkg.core import Widget\n\n__all__ = [\"Widget\"]\n",
            "src/mypkg/core.py": "class Widget:\n    pass\n",
            "src/mypkg/extra.py": "def src_dead():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={
            "exit": 1,
            "stdout": (
                '[{"code": "F401", "message": "`mypkg.core.Widget` imported but unused", '
                '"filename": "src/mypkg/__init__.py", "location": {"row": 1, "column": 24}}]'
            ),
        },
        vulture=vulture(("src/mypkg/extra.py", 1, "function", "src_dead")),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    finding = next(f for f in out.findings if f.symbol == "src_dead")
    assert finding.recommendation is Recommendation.CANDIDATE
    assert finding.project == ".", finding.project
    # src/ layout: ``src/mypkg`` is the top-level package, so its __init__ is
    # the published surface, exactly as in a flat layout (PY-1/PY-14).
    widget = next(f for f in out.findings if f.symbol == "Widget")
    assert widget.file == "src/mypkg/__init__.py"
    assert widget.contract is Contract.PUBLIC, widget.contract_reasons
    assert widget.recommendation is Recommendation.PROTECTED


def test_distributed_library_with_a_cli_keeps_its_library_surface(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": (
                PYPROJECT.format(name="mylib")
                + '\n[project.scripts]\nmylib-cli = "mylib.cli:main"\n'
            ),
            "mylib/__init__.py": "from mylib.core import Widget\n",
            "mylib/core.py": "class Widget:\n    pass\n",
            "mylib/cli.py": "def main():\n    return 0\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={
            "exit": 1,
            "stdout": (
                '[{"code": "F401", "message": "`mylib.core.Widget` imported but unused", '
                '"filename": "mylib/__init__.py", "location": {"row": 1, "column": 24}}]'
            ),
        },
        vulture=vulture(("mylib/cli.py", 1, "function", "main")),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    widget = next(f for f in out.findings if f.symbol == "Widget")
    assert widget.contract is Contract.PUBLIC, widget.contract_reasons
    assert widget.recommendation is Recommendation.PROTECTED
    cli_main = next(f for f in out.findings if f.symbol == "main")
    assert cli_main.recommendation is Recommendation.PROTECTED


def test_published_all_surface_is_public(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="acme-orders"),
            "acme_orders/__init__.py": (
                "from acme_orders.core import Order\n\n__all__ = [\"Order\"]\n"
            ),
            "acme_orders/core.py": "class Order:\n    pass\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={
            "exit": 1,
            "stdout": (
                '[{"code": "F401", "message": "`acme_orders.core.Order` imported but unused", '
                '"filename": "acme_orders/__init__.py", "location": {"row": 1, "column": 27}}]'
            ),
        },
        vulture={"exit": 0, "stdout": ""},
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    order = next(f for f in out.findings if f.symbol == "Order")
    assert order.contract is Contract.PUBLIC, order.contract_reasons
    assert order.recommendation is Recommendation.PROTECTED


def test_private_symbol_in_init_is_not_public(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="mylib"),
            "mylib/__init__.py": "from mylib.core import _internal_helper\n",
            "mylib/core.py": "def _internal_helper():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={
            "exit": 1,
            "stdout": (
                '[{"code": "F401", "message": "`mylib.core._internal_helper` imported but unused", '
                '"filename": "mylib/__init__.py", "location": {"row": 1, "column": 24}}]'
            ),
        },
        vulture={"exit": 0, "stdout": ""},
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    finding = next(f for f in out.findings if f.symbol == "_internal_helper")
    assert finding.contract is not Contract.PUBLIC, (
        "a leading-underscore name is private by convention, wherever it is re-exported"
    )
    assert finding.recommendation is not Recommendation.CANDIDATE


def test_python_project_respects_its_own_pyproject_configuration(tmp_path):
    """Analyzers run from the project root, never from the repo root or isolated."""
    repo = write_repo(
        tmp_path,
        {
            "backend/pyproject.toml": (
                PYPROJECT.format(name="backend")
                + '\n[tool.ruff]\nline-length = 120\n'
            ),
            "backend/app/main.py": "def backend_dead():\n    return 1\n",
        },
    )
    recorded = tmp_path / "cwd.txt"
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]", "record_cwd": str(recorded)},
        vulture={"exit": 0, "stdout": ""},
    )

    run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    assert recorded.is_file(), "the analyzer stub never ran"
    cwd = recorded.read_text(encoding="utf-8").strip().replace("\\", "/")
    assert cwd.endswith("/backend"), cwd


# --------------------------------------------------------------------------- #
# TESTS: environment independence and honest reporting
# --------------------------------------------------------------------------- #


def test_strict_gate_is_independent_of_installed_tools(tmp_path):
    """The strict gate must not depend on which analyzers this host happens to have."""
    from dead_code_hunter import cli

    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="env-independent"),
            "app/__init__.py": "",
            "app/core.py": "def helper():\n    return 1\n",
        },
    )
    overrides = ToolOverrides()
    overrides.commands["ruff"] = stub_command(tmp_path, "ruff", {"exit": 0, "stdout": "[]"})
    overrides.commands["vulture"] = stub_command(tmp_path, "vulture", {"exit": 0, "stdout": ""})

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))
    assert {entry["coverage"] for entry in coverage_entries(out)} == {Coverage.COMPLETE.value}

    import dead_code_hunter.scanner as scanner_module

    original = scanner_module.ScanOptions

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        cli,
        "ScanOptions",
        lambda **kwargs: original(
            **{**kwargs, "overrides": overrides, "write_reports": False}
        ),
        raising=True,
    )
    try:
        code = cli.main(["scan", "--strict", "--repo", str(repo), "--no-write", "--format", "none"])
    finally:
        monkey.undo()
    assert code == 0, "complete coverage must pass the strict gate regardless of host tools"


def test_git_integrity_declares_ignored_files_are_not_tracked(tmp_path):
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="git-ignored"),
            "app/__init__.py": "",
            "app/core.py": "def helper():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path, ruff={"exit": 0, "stdout": "[]"}, vulture={"exit": 0, "stdout": ""}
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    git = out.report["git_integrity"]
    assert git["ignored_files_tracked"] is False
    assert "gitignore" in git["ignored_files_note"].lower()


# --------------------------------------------------------------------------- #
# PYTHON: real-analyzer fidelity found during remediation
# --------------------------------------------------------------------------- #


def test_vulture_parse_error_is_partial_even_when_exit_code_is_success(tmp_path):
    """Vulture exits 3 when it also found something, hiding the unparsable file.

    Verified against vulture 2.14: a syntax error is reported on stderr as
    ``path.py:line: invalid syntax at "..."`` while the process still exits 3.
    Trusting the exit code alone would report a COMPLETE scan over a file that
    was never analysed (BUG-03, spec PY-2).
    """
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="vulture-masked"),
            "app/__init__.py": "",
            "app/broken.py": "def broken(:\n    return 1\n",
            "app/ok.py": "def real_dead():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture={
            "exit": 3,
            "stdout": "app/ok.py:1: unused function 'real_dead' (60% confidence)\n",
            "stderr": 'app/broken.py:1: invalid syntax at "def broken(:"\n',
        },
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    entry = coverage_for(out, "vulture")
    assert entry["coverage"] == Coverage.PARTIAL.value, entry["reason"]
    assert "app/broken.py" in entry["reason"]
    finding = next(f for f in out.findings if f.symbol == "real_dead")
    assert finding.recommendation is not Recommendation.CANDIDATE


def test_vulture_finding_lines_are_not_mistaken_for_parse_errors(tmp_path):
    """A clean run with findings must stay COMPLETE."""
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="vulture-clean"),
            "app/__init__.py": "",
            "app/ok.py": "def real_dead():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture={
            "exit": 3,
            "stdout": "app/ok.py:1: unused function 'real_dead' (60% confidence)\n",
        },
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    entry = coverage_for(out, "vulture")
    assert entry["coverage"] == Coverage.COMPLETE.value, entry["reason"]
    finding = next(f for f in out.findings if f.symbol == "real_dead")
    assert finding.recommendation is Recommendation.CANDIDATE


def test_scope_does_not_change_recommendations_inside_the_scope(tmp_path):
    """Same code, same verdict: scoping must not weaken evidence."""
    files = {
        "pyproject.toml": PYPROJECT.format(name="scope-stable"),
        "app/__init__.py": "",
        "app/core.py": "def shared_helper():\n    return 1\n",
        "app/use.py": "from app.core import shared_helper\n\nprint(shared_helper())\n",
        "app/dead.py": "def scope_dead():\n    return 2\n",
    }
    repo = write_repo(tmp_path, files)
    overrides = tool_overrides(
        tmp_path,
        ruff={"exit": 0, "stdout": "[]"},
        vulture=vulture(
            ("app/core.py", 1, "function", "shared_helper"),
            ("app/dead.py", 1, "function", "scope_dead"),
        ),
    )

    full = run_scan(repo, tmp_path, overrides=overrides, langs=("python",), scan_id="full")
    scoped = run_scan(
        repo, tmp_path, overrides=overrides, langs=("python",), scope=["app/core.py"], scan_id="scoped"
    )

    full_map = {f.symbol: f.recommendation for f in full.findings}
    scoped_map = {f.symbol: f.recommendation for f in scoped.findings}
    assert scoped_map == {"shared_helper": full_map["shared_helper"]}
    assert scoped_map["shared_helper"] is Recommendation.RETAIN


# --------------------------------------------------------------------------- #
# REPORT: finding identity must survive deduplication
# --------------------------------------------------------------------------- #


def test_findings_keep_their_identity_after_deduplication(tmp_path):
    """Every reported finding needs a stable id, fingerprint and scan_id.

    Two analyzers reporting the same item are merged into one finding; the
    merged object must still carry the identity used by approvals and reports.
    """
    repo = write_repo(
        tmp_path,
        {
            "pyproject.toml": PYPROJECT.format(name="identity"),
            "app/__init__.py": "",
            "app/utils.py": "import json\n\n\ndef helper():\n    return 1\n",
        },
    )
    overrides = tool_overrides(
        tmp_path,
        ruff={
            "exit": 1,
            "stdout": (
                '[{"code": "F401", "message": "`json` imported but unused", '
                '"filename": "app/utils.py", "location": {"row": 1, "column": 8}}]'
            ),
        },
        vulture=vulture(("app/utils.py", 1, "import", "json")),
    )

    out = run_scan(repo, tmp_path, overrides=overrides, langs=("python",))

    assert out.report["deduplicated"] >= 1, "the fixture must actually exercise the merge"
    assert out.findings
    for finding in out.findings:
        assert finding.id, f"{finding.symbol} lost its id during deduplication"
        assert finding.fingerprint, f"{finding.symbol} lost its fingerprint"
        assert finding.scan_id == out.scan_id
    payload = out.report["findings"]
    ids = [item["id"] for item in payload]
    assert all(ids), f"a report finding has no id: {ids}"
    assert len(ids) == len(set(ids)), f"duplicate finding ids: {ids}"
