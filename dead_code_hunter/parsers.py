"""Parsers for each analyzer's raw output.

Parsers are pure functions: text in, normalized drafts out.  They never touch
the filesystem or the network, which keeps the failure-mode tests honest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .models import Category


@dataclass
class RawItem:
    """A normalized analyzer item before it becomes a :class:`Finding`."""

    kind: str
    category: Category
    file: str
    line: int | None = None
    end_line: int | None = None
    symbol: str | None = None
    message: str = ""
    signal: str = ""


@dataclass
class ParseOutcome:
    items: list[RawItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parsed: bool = False


_RUFF_CODE_SYMBOL = re.compile(r"`([^`]+)`")
_RUFF_KINDS = {
    "F401": "import",
    "F811": "redefinition",
    "F841": "local",
    "F821": "undefined_name",
}


def parse_ruff(stdout: str) -> ParseOutcome:
    """Parse ``ruff check --output-format json``."""
    outcome = ParseOutcome()
    text = stdout.strip()
    if not text:
        outcome.errors.append("ruff produced no JSON output")
        return outcome
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        outcome.errors.append(f"ruff output is not valid JSON: {exc}")
        return outcome
    if isinstance(data, dict):  # ruff reports its own error this way
        outcome.errors.append(f"ruff error: {data.get('message') or data}")
        return outcome
    if not isinstance(data, list):
        outcome.errors.append("ruff JSON output is not a list")
        return outcome
    outcome.parsed = True
    for entry in data:
        if not isinstance(entry, dict):
            outcome.warnings.append("ruff item is not an object; skipped")
            continue
        code = str(entry.get("code") or "")
        filename = entry.get("filename") or entry.get("file")
        if not isinstance(filename, str) or not filename:
            outcome.warnings.append("ruff item without filename; skipped")
            continue
        message = str(entry.get("message") or "")
        match = _RUFF_CODE_SYMBOL.search(message)
        symbol = match.group(1).strip() if match else None
        if symbol and " as " in symbol:
            symbol = symbol.split(" as ")[-1].strip()
        if symbol:
            symbol = symbol.split(".")[-1].strip()
        location = entry.get("location") if isinstance(entry.get("location"), dict) else {}
        end_location = entry.get("end_location") if isinstance(entry.get("end_location"), dict) else {}
        line = location.get("row") if isinstance(location.get("row"), int) else None
        end_line = end_location.get("row") if isinstance(end_location.get("row"), int) else None
        outcome.items.append(
            RawItem(
                kind=_RUFF_KINDS.get(code, "lint"),
                category=Category.DEAD_CODE,
                file=filename,
                line=line,
                end_line=end_line,
                symbol=symbol,
                message=message,
                signal=f"ruff:{code}",
            )
        )
    return outcome


_VULTURE_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):\s*(?P<message>.*?)(?:\s*\((?P<confidence>\d+)% confidence\))?\s*$"
)
_VULTURE_KIND = re.compile(r"unused\s+(?P<kind>[a-z ]+?)\s+'(?P<name>[^']+)'")
_VULTURE_KIND_MAP = {
    "function": "function",
    "class": "class",
    "import": "import",
    "variable": "variable",
    "attribute": "attribute",
    "method": "method",
    "property": "property",
    "class attribute": "class_attribute",
}


def parse_vulture(stdout: str) -> ParseOutcome:
    """Parse vulture's ``path:line: message (NN% confidence)`` lines."""
    outcome = ParseOutcome()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        match = _VULTURE_LINE.match(line.strip())
        if not match:
            outcome.warnings.append(f"unparsed vulture line: {line.strip()[:120]}")
            continue
        message = match.group("message").strip()
        kind_match = _VULTURE_KIND.search(message)
        if kind_match:
            kind = _VULTURE_KIND_MAP.get(kind_match.group("kind").strip(), "unknown")
            symbol = kind_match.group("name")
        else:
            kind = "unknown"
            symbol = None
            outcome.warnings.append(f"unrecognized vulture message: {message[:120]}")
        outcome.items.append(
            RawItem(
                kind=kind,
                category=Category.DEAD_CODE,
                file=match.group("file"),
                line=int(match.group("line")),
                symbol=symbol,
                message=message,
                signal=f"vulture:{kind}",
            )
        )
    outcome.parsed = True
    return outcome


KNIP_KIND_MAP = {
    "files": ("file", Category.DEAD_CODE),
    "exports": ("export", Category.DEAD_CODE),
    "types": ("type", Category.DEAD_CODE),
    "nsExports": ("ns_export", Category.DEAD_CODE),
    "nsTypes": ("ns_type", Category.DEAD_CODE),
    "enumMembers": ("enum_member", Category.DEAD_CODE),
    "classMembers": ("class_member", Category.DEAD_CODE),
    "dependencies": ("dependency", Category.DEPENDENCY_ISSUE),
    "devDependencies": ("dev_dependency", Category.DEPENDENCY_ISSUE),
    "optionalPeerDependencies": ("optional_peer_dependency", Category.DEPENDENCY_ISSUE),
    "unlisted": ("unlisted", Category.DEPENDENCY_ISSUE),
    "unresolved": ("unresolved", Category.DEPENDENCY_ISSUE),
    "binaries": ("binary", Category.DEPENDENCY_ISSUE),
    "catalog": ("catalog", Category.DEPENDENCY_ISSUE),
    "duplicates": ("duplicate", Category.INFO),
}

#: Keys that mean "the analysis could not resolve the graph" (spec DP-5).
KNIP_INCOMPLETE_KEYS = ("unresolved",)


@dataclass
class KnipParseOutcome(ParseOutcome):
    files: list[str] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    configuration_hints: list[dict] = field(default_factory=list)
    entry_files: list[str] = field(default_factory=list)
    has_entry_field: bool = False
    unknown_keys: list[str] = field(default_factory=list)
    per_file_keys: dict[str, list[str]] = field(default_factory=dict)


def _knip_issue_items(file: str, issue: dict, outcome: KnipParseOutcome) -> None:
    for key, value in issue.items():
        if key == "file":
            continue
        mapping = KNIP_KIND_MAP.get(key)
        if mapping is None:
            outcome.unknown_keys.append(key)
            outcome.warnings.append(f"unknown knip issue type '{key}' in {file}; reported as info")
            kind, category = ("unknown", Category.INFO)
        else:
            kind, category = mapping
        if not isinstance(value, list):
            continue
        for entry in value:
            if isinstance(entry, str):
                name = entry
                line = None
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("symbol") or entry.get("file")
                line = entry.get("line") if isinstance(entry.get("line"), int) else None
            else:
                continue
            if key in KNIP_INCOMPLETE_KEYS:
                outcome.unresolved.append(
                    {"file": file, "name": name, "line": line}
                )
            outcome.items.append(
                RawItem(
                    kind=kind,
                    category=category,
                    file=file,
                    line=line,
                    symbol=str(name) if name else None,
                    message=f"knip {key}",
                    signal=f"knip:{key}",
                )
            )


def parse_knip(stdout: str) -> KnipParseOutcome:
    """Parse ``knip --reporter json`` output, including failure shapes."""
    outcome = KnipParseOutcome()
    text = stdout.strip()
    if not text:
        outcome.errors.append("knip produced no JSON output")
        return outcome
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        outcome.errors.append(f"knip output is not valid JSON: {exc}")
        return outcome

    documents: list[dict] = []
    if isinstance(data, list):
        documents = [d for d in data if isinstance(d, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("workspaces"), dict):
            for name, workspace in data["workspaces"].items():
                if isinstance(workspace, dict):
                    workspace.setdefault("_workspace", name)
                    documents.append(workspace)
        else:
            documents.append(data)
    else:
        outcome.errors.append("knip JSON output has an unexpected shape")
        return outcome
    if not documents:
        outcome.errors.append("knip JSON output contained no workspace results")
        return outcome

    outcome.parsed = True
    for document in documents:
        files = document.get("files")
        if isinstance(files, list):
            for file in files:
                if isinstance(file, str):
                    outcome.files.append(file)
                    outcome.items.append(
                        RawItem(
                            kind="file",
                            category=Category.DEAD_CODE,
                            file=file,
                            symbol=file,
                            message="knip files: unused file",
                            signal="knip:files",
                        )
                    )
        issues = document.get("issues")
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                file = issue.get("file")
                if not isinstance(file, str):
                    continue
                outcome.per_file_keys.setdefault(file, []).extend(
                    k for k in issue if k not in {"file"}
                )
                _knip_issue_items(file, issue, outcome)
        hints = document.get("configurationHints")
        if isinstance(hints, list):
            outcome.configuration_hints.extend(h for h in hints if isinstance(h, dict))
        entry = document.get("entry")
        if isinstance(entry, list):
            outcome.has_entry_field = True
            outcome.entry_files.extend(e for e in entry if isinstance(e, str))
    return outcome


DART_MACHINE = re.compile(
    r"^(?P<severity>[A-Z]+)\|(?P<type>[A-Z_]+)\|(?P<code>[A-Za-z0-9_]+)\|"
    r"(?P<file>[^|]*)\|(?P<line>\d+)\|(?P<col>\d+)\|(?P<length>\d+)\|(?P<message>.*)$"
)
_DART_UNUSED_CODES = {
    "unused_import": "import",
    "unused_element": "element",
    "unused_field": "field",
    "unused_local_variable": "local",
    "unused_catch_clause": "catch_clause",
    "unused_label": "label",
    "unused_shown_name": "import",
    "dead_code": "unreachable_code",
    "dead_code_on_catch_subtype": "unreachable_code",
    "unused_result": "unused_result",
}
DART_INCOMPLETE_CODES = {
    "uri_does_not_exist",
    "uri_has_not_been_generated",
    "undefined_class",
    "undefined_function",
    "undefined_identifier",
    "undefined_method",
    "undefined_getter",
    "undefined_setter",
    "undefined_name",
}


@dataclass
class DartDiagnostic:
    severity: str
    type: str
    code: str
    file: str
    line: int
    column: int
    message: str


@dataclass
class DartParseOutcome(ParseOutcome):
    diagnostics: list[DartDiagnostic] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0


def parse_dart(stdout: str) -> DartParseOutcome:
    """Parse ``dart analyze --format=machine`` lines."""
    outcome = DartParseOutcome()
    outcome.parsed = True
    for line in stdout.splitlines():
        if not line.strip():
            continue
        match = DART_MACHINE.match(line.strip())
        if not match:
            outcome.warnings.append(f"unparsed dart analyze line: {line.strip()[:120]}")
            continue
        diagnostic = DartDiagnostic(
            severity=match.group("severity"),
            type=match.group("type"),
            code=match.group("code"),
            file=match.group("file"),
            line=int(match.group("line")),
            column=int(match.group("col")),
            message=match.group("message"),
        )
        outcome.diagnostics.append(diagnostic)
        if diagnostic.severity == "ERROR":
            outcome.error_count += 1
        elif diagnostic.severity == "WARNING":
            outcome.warning_count += 1
        else:
            outcome.info_count += 1
        kind = _DART_UNUSED_CODES.get(diagnostic.code)
        if kind:
            outcome.items.append(
                RawItem(
                    kind=kind,
                    category=Category.DEAD_CODE,
                    file=diagnostic.file,
                    line=diagnostic.line,
                    end_line=diagnostic.line,
                    symbol=_dart_symbol(diagnostic.message),
                    message=diagnostic.message,
                    signal=f"dart:{diagnostic.code}",
                )
            )
    return outcome


def _dart_symbol(message: str) -> str | None:
    for pattern in (
        r"'([^']+)' isn't used",
        r"The declaration '([^']+)' isn't referenced",
        r"The value of the field '([^']+)' isn't used",
        r"The value of the local variable '([^']+)' isn't used",
        r"unused label '([^']+)'",
        r"'([^']+)' isn't a type",
    ):
        match = re.search(pattern, message)
        if match:
            return match.group(1)
    match = re.search(r"'([^']+)'", message)
    return match.group(1) if match else None
