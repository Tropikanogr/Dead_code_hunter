"""Unit tests for the reference analyses (X-3 patterns, DT-2 import graph)."""

from __future__ import annotations

from pathlib import Path

from dead_code_hunter.analysis import dart_refs, js_refs, python_refs
from dead_code_hunter.analysis.entrypoints import discover_entrypoints
from dead_code_hunter.discovery import discover
from dead_code_hunter.models import Category, Contract, Coverage, Finding, Impact


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def python_facts(repo: Path):
    discovery = discover(repo)
    projects = [p.path for p in discovery.projects if "python" in p.langs] or ["."]
    return python_refs.build_facts(repo, discovery.all_files, projects)


def dart_finding(file: str, symbol: str | None = None, kind: str = "file") -> Finding:
    return Finding(
        tool="dart-import-graph",
        lang="dart",
        project=".",
        kind=kind,
        category=Category.DEAD_CODE,
        file=file,
        line=1,
        symbol=symbol or file,
        impact=Impact.LOW,
    )


class TestPythonReferences:
    def test_all_listed_name_is_internal_shared(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pkg/__init__.py", 'from pkg.core import Thing\n\n__all__ = ["Thing"]\n')
        write(repo / "pkg/core.py", "class Thing:\n    pass\n")
        facts = python_facts(repo)
        finding = Finding(
            tool="ruff", lang="python", project=".", kind="import",
            category=Category.DEAD_CODE, file="pkg/__init__.py", line=1, symbol="Thing",
        )
        python_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.contract is Contract.INTERNAL_SHARED
        assert any("__all__" in reason for reason in finding.contract_reasons)

    def test_module_attribute_access_is_a_reference(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pkg/__init__.py", "")
        write(repo / "pkg/core.py", "def build():\n    return 1\n")
        write(repo / "pkg/use.py", "import pkg.core\n\npkg.core.build()\n")
        facts = python_facts(repo)
        finding = Finding(
            tool="vulture", lang="python", project=".", kind="function",
            category=Category.DEAD_CODE, file="pkg/core.py", line=1, symbol="build",
        )
        python_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.reference.value == "active"

    def test_importlib_reference_marks_the_module_reachable(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pkg/__init__.py", "")
        write(repo / "pkg/plugins.py", "def run():\n    return 1\n")
        write(repo / "pkg/loader.py", "import importlib\n\nimportlib.import_module('pkg.plugins')\n")
        facts = python_facts(repo)
        finding = Finding(
            tool="vulture", lang="python", project=".", kind="function",
            category=Category.DEAD_CODE, file="pkg/plugins.py", line=1, symbol="run",
        )
        python_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.reference.value == "active"

    def test_syntax_error_file_makes_the_index_incomplete(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pkg/__init__.py", "")
        write(repo / "pkg/core.py", "def helper():\n    return 1\n")
        write(repo / "pkg/broken.py", "def broken(:\n")
        facts = python_facts(repo)
        finding = Finding(
            tool="vulture", lang="python", project=".", kind="function",
            category=Category.DEAD_CODE, file="pkg/core.py", line=1, symbol="helper",
        )
        python_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.coverage is Coverage.PARTIAL
        assert any("broken.py" in reason for reason in finding.coverage_reasons)

    def test_dunder_names_are_retained(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pkg/__init__.py", "")
        write(repo / "pkg/core.py", "class Thing:\n    def __eq__(self, other):\n        return True\n")
        facts = python_facts(repo)
        finding = Finding(
            tool="vulture", lang="python", project=".", kind="method",
            category=Category.DEAD_CODE, file="pkg/core.py", line=2, symbol="__eq__",
        )
        python_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.reference.value == "active"


class TestJsReferences:
    def facts(self, repo: Path):
        discovery = discover(repo)
        entrypoints, _ = discover_entrypoints(repo, discovery.all_files)
        return js_refs.build_facts(repo, discovery.all_files, discovery.packages, entrypoints)

    def test_package_json_entry_fields_protect_exports(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "package.json", '{"name": "lib", "main": "src/index.js"}\n')
        write(repo / "src/index.js", "export function api() {}\n")
        facts = self.facts(repo)
        assert facts.entry_files.get("src/index.js")

    def test_nested_expression_exports_are_collected(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "package.json", '{"name": "lib"}\n')
        write(
            repo / "package.json",
            '{"name": "lib", "exports": {".": {"import": "./src/index.mjs"}, "./x": "./src/x.mjs"}}\n',
        )
        write(repo / "src/index.mjs", "export const a = 1;\n")
        write(repo / "src/x.mjs", "export const b = 1;\n")
        facts = self.facts(repo)
        assert {"src/index.mjs", "src/x.mjs"} <= set(facts.entry_files)

    def test_non_literal_dynamic_import_is_recorded(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "package.json", '{"name": "lib"}\n')
        write(repo / "src/index.js", "export function load(name) { return import('./' + name); }\n")
        facts = self.facts(repo)
        assert facts.unresolved_dynamic

    def test_route_and_socket_files_are_detected(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "package.json", '{"name": "lib"}\n')
        write(repo / "src/routes.js", "app.get('/x', handler);\n")
        write(repo / "src/events.js", "socket.on('order:new', handler);\n")
        facts = self.facts(repo)
        assert "src/routes.js" in facts.route_files
        assert "src/events.js" in facts.socket_files


class TestDartReferences:
    def facts(self, repo: Path):
        discovery = discover(repo)
        projects = [p.path for p in discovery.projects if "dart" in p.langs] or ["."]
        return dart_refs.build_facts(repo, discovery.all_files, projects)

    def test_part_and_deferred_directives_create_edges(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pubspec.yaml", "name: app\npublish_to: none\n")
        write(
            repo / "lib/main.dart",
            "import 'deferred/feature.dart' deferred as feature;\n"
            "part 'model.dart';\n"
            "void main() {}\n",
        )
        write(repo / "lib/deferred/feature.dart", "void featureEntry() {}\n")
        write(repo / "lib/model.dart", "part of 'main.dart';\n\nclass Model {}\n")
        facts = self.facts(repo)
        assert "lib/deferred/feature.dart" in facts.edges["lib/main.dart"]
        assert "lib/model.dart" in facts.edges["lib/main.dart"]
        assert dart_refs.orphan_files(facts) == []

    def test_generated_files_are_excluded_from_orphan_detection(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pubspec.yaml", "name: app\npublish_to: none\n")
        write(repo / "lib/main.dart", "void main() {}\n")
        write(repo / "lib/model.g.dart", "class Generated {}\n")
        write(repo / "lib/model.freezed.dart", "class Freezed {}\n")
        facts = self.facts(repo)
        assert dart_refs.orphan_files(facts) == []

    def test_method_channel_file_is_public(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "pubspec.yaml", "name: app\npublish_to: none\n")
        write(repo / "lib/main.dart", "void main() {}\n")
        write(repo / "lib/native/bridge.dart", "final channel = MethodChannel('sync');\n")
        facts = self.facts(repo)
        finding = dart_finding("lib/native/bridge.dart")
        dart_refs.enrich(finding, facts, Coverage.COMPLETE, ".")
        assert finding.contract is Contract.PUBLIC

    def test_top_level_lib_file_of_package_without_main_is_internal_shared(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "packages/core/pubspec.yaml", "name: core\n")
        write(repo / "packages/core/lib/core.dart", "class Core {}\n")
        facts = self.facts(repo)
        finding = dart_finding("packages/core/lib/core.dart")
        dart_refs.enrich(finding, facts, Coverage.COMPLETE, "packages/core")
        assert finding.contract is Contract.INTERNAL_SHARED
