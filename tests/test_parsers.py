"""Parser tests, including the failure shapes each tool can produce."""

from __future__ import annotations

import json

from dead_code_hunter.models import Category
from dead_code_hunter.parsers import parse_dart, parse_knip, parse_ruff, parse_vulture


class TestRuffParser:
    def test_parses_unused_import_and_variable(self):
        payload = json.dumps(
            [
                {
                    "code": "F401",
                    "message": "`json` imported but unused",
                    "filename": "app/utils.py",
                    "location": {"row": 1, "column": 8},
                    "end_location": {"row": 1, "column": 12},
                },
                {
                    "code": "F841",
                    "message": "Local variable `tmp` is assigned to but never used",
                    "filename": "app/utils.py",
                    "location": {"row": 9, "column": 5},
                    "end_location": {"row": 9, "column": 8},
                },
            ]
        )
        outcome = parse_ruff(payload)
        assert outcome.parsed
        assert [item.kind for item in outcome.items] == ["import", "local"]
        assert [item.symbol for item in outcome.items] == ["json", "tmp"]
        assert outcome.items[0].file == "app/utils.py"
        assert outcome.items[0].line == 1
        assert outcome.items[0].signal == "ruff:F401"

    def test_relative_symbol_in_import_message_is_reduced(self):
        payload = json.dumps(
            [
                {
                    "code": "F401",
                    "message": "`pkg.models.Order` imported but unused",
                    "filename": "pkg/__init__.py",
                    "location": {"row": 1, "column": 25},
                }
            ]
        )
        outcome = parse_ruff(payload)
        assert outcome.items[0].symbol == "Order"

    def test_error_payload_is_reported_not_parsed_as_findings(self):
        outcome = parse_ruff(json.dumps({"message": "Failed to parse pyproject.toml"}))
        assert not outcome.parsed
        assert outcome.errors
        assert outcome.items == []

    def test_invalid_json_is_reported(self):
        outcome = parse_ruff("Traceback (most recent call last):")
        assert not outcome.parsed
        assert "not valid JSON" in outcome.errors[0]

    def test_empty_output_is_an_error(self):
        outcome = parse_ruff("   \n")
        assert not outcome.parsed and outcome.errors


class TestVultureParser:
    def test_parses_kinds_and_confidence(self):
        text = (
            "app/core.py:4: unused function 'calculate_internal_margin' (60% confidence)\n"
            "app/models.py:2: unused class 'Legacy' (60% confidence)\n"
            "app/core.py:1: unused import 'json' (90% confidence)\n"
            "app/models.py:7: unused attribute 'name' (60% confidence)\n"
            "app/models.py:9: unused method 'save' (60% confidence)\n"
            "app/models.py:11: unused property 'total' (60% confidence)\n"
            "app/models.py:13: unused variable 'tmp' (60% confidence)\n"
        )
        outcome = parse_vulture(text)
        assert [item.kind for item in outcome.items] == [
            "function",
            "class",
            "import",
            "attribute",
            "method",
            "property",
            "variable",
        ]
        assert outcome.items[0].symbol == "calculate_internal_margin"
        assert outcome.items[0].line == 4
        assert outcome.items[0].category is Category.DEAD_CODE

    def test_unknown_line_is_a_warning_not_a_finding(self):
        outcome = parse_vulture("this is not vulture output\n")
        assert outcome.items == []
        assert outcome.warnings

    def test_message_without_confidence_still_parses(self):
        outcome = parse_vulture("app/core.py:4: unused function 'helper'\n")
        assert outcome.items[0].symbol == "helper"


class TestKnipParser:
    def test_classification_of_every_documented_key(self):
        payload = json.dumps(
            {
                "files": ["src/orphan.ts"],
                "issues": [
                    {
                        "file": "src/a.ts",
                        "exports": [{"name": "unusedExport", "line": 3, "col": 1, "pos": 10}],
                        "types": [{"name": "UnusedType", "line": 5, "col": 1, "pos": 20}],
                        "dependencies": [{"name": "lodash", "line": 1, "col": 1, "pos": 0}],
                        "devDependencies": [{"name": "jest", "line": 2, "col": 1, "pos": 0}],
                        "optionalPeerDependencies": [{"name": "ws", "line": 3, "col": 1, "pos": 0}],
                        "unlisted": [{"name": "left-pad", "line": 4, "col": 1, "pos": 0}],
                        "unresolved": [{"name": "missing-pkg", "line": 5, "col": 1, "pos": 0}],
                        "binaries": [{"name": "tsc", "line": 6, "col": 1, "pos": 0}],
                        "catalog": [{"name": "react", "line": 7, "col": 1, "pos": 0}],
                        "duplicates": [{"name": "dup", "line": 8, "col": 1, "pos": 0}],
                        "nsExports": [{"name": "nsExport", "line": 9, "col": 1, "pos": 0}],
                        "nsTypes": [{"name": "nsType", "line": 10, "col": 1, "pos": 0}],
                        "enumMembers": [{"name": "ENUM_MEMBER", "line": 11, "col": 1, "pos": 0}],
                        "classMembers": [{"name": "classMember", "line": 12, "col": 1, "pos": 0}],
                    }
                ],
            }
        )
        outcome = parse_knip(payload)
        assert outcome.parsed
        by_symbol = {item.symbol: item for item in outcome.items}
        dead = {
            "unusedExport", "UnusedType", "nsExport", "nsType", "ENUM_MEMBER",
            "classMember", "src/orphan.ts",
        }
        assert dead <= set(by_symbol)
        for symbol in dead:
            assert by_symbol[symbol].category is Category.DEAD_CODE, symbol
        # Dependency problems are never dead code (BUG-01 / JS-3).
        for symbol in ("lodash", "jest", "ws", "left-pad", "missing-pkg", "tsc", "react"):
            assert by_symbol[symbol].category is Category.DEPENDENCY_ISSUE, symbol
        assert by_symbol["dup"].category is Category.INFO
        assert {u["name"] for u in outcome.unresolved} == {"missing-pkg"}

    def test_unknown_key_is_info_with_warning(self):
        payload = json.dumps(
            {"files": [], "issues": [{"file": "src/a.ts", "brandNewKey": [{"name": "x"}]}]}
        )
        outcome = parse_knip(payload)
        assert outcome.unknown_keys == ["brandNewKey"]
        assert outcome.items[0].category is Category.INFO
        assert outcome.warnings

    def test_workspace_wrapped_output(self):
        payload = json.dumps(
            {
                "workspaces": {
                    "packages/a": {"files": [], "issues": [{"file": "packages/a/src/x.ts", "exports": [{"name": "a"}]}]},
                    "packages/b": {"files": ["packages/b/src/y.ts"], "issues": []},
                }
            }
        )
        outcome = parse_knip(payload)
        assert {item.symbol for item in outcome.items} == {"a", "packages/b/src/y.ts"}

    def test_configuration_hints_are_collected(self):
        payload = json.dumps(
            {"files": [], "issues": [], "configurationHints": [{"identifier": "plugin", "type": "missing"}]}
        )
        outcome = parse_knip(payload)
        assert outcome.configuration_hints

    def test_error_output_is_reported(self):
        outcome = parse_knip("Error: Cannot find module 'typescript'")
        assert not outcome.parsed and outcome.errors


class TestDartParser:
    def test_machine_format_and_unused_codes(self):
        text = (
            "INFO|LINT|unused_import|lib/a.dart|1|8|6|Unused import: 'b.dart'.\n"
            "INFO|HINT|unused_element|lib/b.dart|4|7|10|The declaration 'Helper' isn't referenced.\n"
            "ERROR|COMPILE_TIME_ERROR|uri_does_not_exist|lib/c.dart|2|8|20|Target of URI doesn't exist.\n"
            "WARNING|STATIC_WARNING|dead_code|lib/d.dart|9|5|4|Dead code.\n"
        )
        outcome = parse_dart(text)
        assert outcome.error_count == 1
        assert outcome.warning_count == 1
        assert outcome.info_count == 2
        kinds = {item.symbol: item.kind for item in outcome.items}
        assert kinds["Helper"] == "element"
        dead = [item for item in outcome.items if item.signal == "dart:dead_code"]
        assert dead and dead[0].kind == "unreachable_code"

    def test_garbage_lines_are_warnings(self):
        outcome = parse_dart("Analyzing fdt07...\n")
        assert outcome.diagnostics == []
        assert outcome.warnings
