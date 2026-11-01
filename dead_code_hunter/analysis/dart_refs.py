"""Dart/Flutter import graph and reference analysis.

``dart analyze`` does not detect unreachable files, so the scanner builds the
import graph itself: all branches of conditional imports, ``part``/``part of``,
``export`` and ``deferred as``; ``package:`` URIs resolve to other packages of
the same repository (spec DT-2).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import (
    Confidence,
    Contract,
    Coverage,
    Finding,
    Impact,
    ReferenceKind,
    is_common_word,
    normalize_path,
)
from .pathfacts import is_test_file as _is_test_file
from .sources import SourceCache

_DIRECTIVE = re.compile(r"^\s*(?P<kind>import|export|part)\s+(?P<body>[^;]+);", re.MULTILINE)
_QUOTED = re.compile(r"['\"](?P<uri>[^'\"]+)['\"]")
_PART_OF = re.compile(r"^\s*part\s+of\s+", re.MULTILINE)
_MAIN = re.compile(r"^\s*(?:Future<[^>]*>|void|int)\s+main\s*\(", re.MULTILINE)
_ENTRY_PRAGMA = re.compile(r"@pragma\(\s*['\"]vm:entry-point['\"]\s*\)")
_CHANNEL = re.compile(r"\b(?:MethodChannel|EventChannel|BasicMessageChannel)\s*\(")
_GENERATED_HEADER = re.compile(r"GENERATED CODE|DO NOT MODIFY", re.IGNORECASE)
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_STRING_OR_COMMENT = re.compile(
    r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL
)

GENERATED_SUFFIXES = (".g.dart", ".freezed.dart", ".gr.dart", ".mapper.dart", ".module.dart")
TEST_DIRS = ("test/", "integration_test/", "test_driver/")
ENTRY_DIRS = ("bin/", "tool/", "example/", "web/", "linux/", "macos/", "windows/", "android/", "ios/")


@dataclass
class DartPackage:
    path: str
    name: str | None
    publish_to_none: bool
    is_flutter: bool
    deps: dict[str, str] = field(default_factory=dict)  # name -> path (local deps only)


@dataclass
class DartFacts:
    root: Path
    packages: list[DartPackage] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    edges: dict[str, set[str]] = field(default_factory=dict)
    reverse: dict[str, set[str]] = field(default_factory=dict)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    entry_files: set[str] = field(default_factory=set)
    test_reachable: set[str] = field(default_factory=set)
    production_reachable: set[str] = field(default_factory=set)
    test_only: set[str] = field(default_factory=set)
    generated: set[str] = field(default_factory=set)
    pragma_files: set[str] = field(default_factory=set)
    channel_files: set[str] = field(default_factory=set)
    text_occurrences: dict[str, set[str]] = field(default_factory=dict)

    def package_of(self, file: str) -> DartPackage | None:
        best: DartPackage | None = None
        for package in self.packages:
            if package.path == "." or file.startswith(f"{package.path}/"):
                if best is None or len(package.path) > len(best.path):
                    best = package
        return best


def _parse_pubspec(text: str) -> tuple[str | None, bool, bool, dict[str, str]]:
    name: str | None = None
    publish_to_none = False
    is_flutter = False
    deps: dict[str, str] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^[A-Za-z_][\w]*\s*:", line):
            key = line.split(":", 1)[0].strip()
            value = line.split(":", 1)[1].strip()
            if key == "name":
                name = value.strip("\"'")
            elif key == "publish_to":
                publish_to_none = value.strip("\"'") == "none"
            elif key in {"dependencies", "dev_dependencies", "flutter"} and not value:
                section = key
                if key == "flutter":
                    is_flutter = True
                continue
            else:
                section = None
            continue
        if section in {"dependencies", "dev_dependencies"}:
            indent = len(raw) - len(raw.lstrip())
            stripped = line.strip()
            if indent >= 2 and re.match(r"^[A-Za-z_][\w]*\s*:", stripped):
                dep = stripped.split(":", 1)[0].strip()
                value = stripped.split(":", 1)[1].strip()
                deps.setdefault(dep, value)
            elif "path:" in stripped and indent >= 4:
                path_value = stripped.split("path:", 1)[1].strip().strip("\"'")
                deps[list(deps)[-1] if deps else ""] = path_value
        elif section == "flutter":
            if "uses-material-design" in line:
                is_flutter = True
    return name, publish_to_none, is_flutter, deps


def _is_broken_local_reference(uri: str, package_index: dict[str, DartPackage]) -> bool:
    """True when a URI should resolve inside the repository but does not.

    ``dart:`` and third-party ``package:`` URIs are resolved by the package
    manager, not by the repository graph, so they are not analysis gaps.
    """
    if uri.startswith(("dart:", "http:", "https:")):
        return False
    if uri.startswith("package:"):
        name = uri[len("package:"):].split("/", 1)[0]
        return name in package_index
    return True


def _resolve_uri(
    uri: str, from_file: str, package_index: dict[str, DartPackage], fileset: set[str]
) -> str | None:
    if uri.startswith("dart:") or "://" in uri:
        return None
    if uri.startswith("package:"):
        rest = uri[len("package:"):]
        if "/" not in rest:
            return None
        package_name, relative = rest.split("/", 1)
        package = package_index.get(package_name)
        if package is None:
            return None
        candidate = normalize_path(
            f"{package.path}/lib/{relative}" if package.path != "." else f"lib/{relative}"
        )
        return candidate if candidate in fileset else None
    base_dir = str(Path(from_file).parent).replace("\\", "/")
    if base_dir in ("", "."):
        base_dir = ""
    candidate = normalize_path(posixpath.normpath(f"{base_dir}/{uri}" if base_dir else uri))
    return candidate if candidate in fileset else None


def build_facts(root: Path, files: list[str], project_paths: list[str]) -> DartFacts:
    cache = SourceCache(root)
    facts = DartFacts(root=root)
    dart_files = [f for f in files if f.endswith(".dart")]
    facts.files = dart_files
    fileset = set(dart_files)

    for project in sorted(set(project_paths)):
        pubspec = (root / project / "pubspec.yaml") if project != "." else (root / "pubspec.yaml")
        if not pubspec.is_file():
            facts.packages.append(DartPackage(path=project, name=None, publish_to_none=False, is_flutter=False))
            continue
        try:
            text = pubspec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        name, publish_to_none, is_flutter, deps = _parse_pubspec(text)
        facts.packages.append(
            DartPackage(path=project, name=name, publish_to_none=publish_to_none,
                        is_flutter=is_flutter, deps=deps)
        )
    package_index = {p.name: p for p in facts.packages if p.name}

    for rel in dart_files:
        text = cache.read(rel)
        if text is None:
            continue
        if rel.endswith(GENERATED_SUFFIXES) or rel.split("/")[-1] in {"l10n.dart"}:
            facts.generated.add(rel)
            continue
        if "l10n/" in rel or "/generated/" in f"/{rel}":
            facts.generated.add(rel)
        head = "\n".join(text.splitlines()[:5])
        if _GENERATED_HEADER.search(head):
            facts.generated.add(rel)
        targets: set[str] = set()
        for directive in _DIRECTIVE.finditer(text):
            kind = directive.group("kind")
            body = directive.group("body")
            if kind == "part" and body.strip().lower().startswith("of"):
                continue  # "part of" declares the part member, not an edge
            for match in _QUOTED.finditer(body):
                uri = match.group("uri")
                target = _resolve_uri(uri, rel, package_index, fileset)
                if target:
                    targets.add(target)
                elif _is_broken_local_reference(uri, package_index):
                    facts.unresolved.append({"file": rel, "uri": uri})
        facts.edges[rel] = targets
        for target in targets:
            facts.reverse.setdefault(target, set()).add(rel)
        if _ENTRY_PRAGMA.search(text):
            facts.pragma_files.add(rel)
        if _CHANNEL.search(text):
            facts.channel_files.add(rel)
        for match in _IDENT.finditer(_STRING_OR_COMMENT.sub(" ", text)):
            facts.text_occurrences.setdefault(match.group(0), set()).add(rel)

    # part files are reached from their owning library
    for rel in dart_files:
        text = cache.read(rel)
        if not text or not _PART_OF.search(text):
            continue
        for parent, targets in facts.edges.items():
            if rel in targets:
                facts.reverse.setdefault(rel, set()).add(parent)

    _compute_reachability(facts)
    return facts


def _library_root(file: str) -> str:
    parts = file.split("/")
    if len(parts) >= 2 and parts[0] == "lib":
        return parts[1]
    return parts[0] if parts else ""


def is_test_file(rel: str) -> bool:
    return _is_test_file(rel)


def _segment_starts(rel: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if rel.startswith(prefix) or f"/{prefix}" in f"/{rel}":
            return True
    return False


def _compute_reachability(facts: DartFacts) -> None:
    production: set[str] = set()
    test_roots: set[str] = set()
    for rel in facts.files:
        lowered = rel.lower()
        if "/.dart_tool/" in f"/{lowered}" or "/build/" in f"/{lowered}":
            continue
        is_test = _segment_starts(rel, TEST_DIRS)
        if is_test:
            test_roots.add(rel)
            continue
        if rel.endswith("lib/main.dart"):
            production.add(rel)
            facts.entry_files.add(rel)
            continue
        if _segment_starts(rel, ENTRY_DIRS):
            production.add(rel)
            facts.entry_files.add(rel)
            continue
        try:
            text = (facts.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _MAIN.search(text):
            production.add(rel)
            facts.entry_files.add(rel)
    facts.production_reachable = _walk(facts, production)
    facts.test_reachable = _walk(facts, test_roots)
    facts.test_only = facts.test_reachable - facts.production_reachable - test_roots


def _walk(facts: DartFacts, roots: set[str]) -> set[str]:
    seen: set[str] = set()
    stack = sorted(roots)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in sorted(facts.edges.get(current, set())):
            if target not in seen:
                stack.append(target)
    return seen


def orphan_files(facts: DartFacts, project: str | None = None) -> list[str]:
    orphans = []
    for rel in facts.files:
        if project not in (None, ".", "") and not (rel == project or rel.startswith(f"{project}/")):
            continue
        if rel in facts.generated or rel in facts.entry_files:
            continue
        if rel in facts.production_reachable:
            continue
        if rel.endswith("lib/main.dart"):
            continue
        if _segment_starts(rel, TEST_DIRS):
            continue
        if not rel.endswith(".dart"):
            continue
        orphans.append(rel)
    return sorted(orphans)


def base_confidence(finding: Finding, facts: DartFacts) -> None:
    if is_common_word(finding.symbol):
        finding.confidence = Confidence.LOW
        finding.confidence_reasons = ["symbol is a common word; no signal is produced for it"]
        return
    signals = " ".join(finding.signals)
    if "unused_local_variable" in signals or "dead_code" in signals:
        finding.confidence = Confidence.HIGH
        finding.confidence_reasons = ["compiler-level fact local to the enclosing body"]
        return
    if "unused_field" in signals or "unused_element" in signals or "unused_import" in signals:
        finding.confidence = Confidence.MEDIUM
        finding.confidence_reasons = ["whole-project analyzer fact (dart analyze)"]
        return
    if finding.kind == "file":
        if finding.file in facts.test_only or finding.file in facts.test_reachable:
            finding.confidence = Confidence.LOW
            finding.confidence_reasons = ["file is reachable only from tests"]
        else:
            finding.confidence = Confidence.MEDIUM
            finding.confidence_reasons = ["file is not reachable from any entrypoint in the import graph"]
        return
    finding.confidence = Confidence.MEDIUM
    finding.confidence_reasons = ["default confidence for dart analysis"]


def enrich(finding: Finding, facts: DartFacts, coverage: Coverage, project: str) -> None:
    symbol = finding.symbol
    base_confidence(finding, facts)

    finding.contract = Contract.NONE
    if finding.file in facts.pragma_files:
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("@pragma('vm:entry-point') in this file")
    elif finding.file in facts.channel_files:
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("MethodChannel/EventChannel name in this file")

    if finding.contract is Contract.NONE and finding.kind == "file":
        package = facts.package_of(finding.file)
        package_root = package.path if package else project
        parent = str(Path(finding.file).parent).replace("\\", "/")
        lib_dir = normalize_path("lib" if package_root in (".", "") else f"{package_root}/lib")
        is_top_level_lib = parent == lib_dir
        has_main = any(rel.startswith(f"{parent}/main.dart") or rel.endswith("lib/main.dart") for rel in facts.files)
        if is_top_level_lib and not has_main:
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append("top-level lib/ file of a package without a main entrypoint")

    active: list[str] = []
    unresolved: list[str] = []
    if symbol:
        appearances = {f for f in facts.text_occurrences.get(symbol, set()) if f != finding.file}
        if appearances:
            active.append(f"identifier referenced in {', '.join(sorted(appearances)[:3])}")
        if re.fullmatch(r"(fromJson|toJson|copyWith|hashCode|toString|operator ==)", symbol):
            active.append("conventional member invoked by frameworks or the runtime")
    if finding.kind == "file":
        importers = {f for f in facts.reverse.get(finding.file, set()) if f != finding.file}
        test_only = finding.file in facts.test_only
        production_importers = {f for f in importers if not is_test_file(f)}
        if production_importers and not test_only:
            active.append(f"imported/exported by {', '.join(sorted(production_importers)[:3])}")
        elif importers or test_only:
            finding.signals = sorted(set(finding.signals) | {"reachable-only-from-tests"})
            finding.confidence = Confidence.LOW
            finding.confidence_reasons = ["file is reachable only from tests"]
    if not active and symbol and not is_common_word(symbol):
        textual = {f for f in facts.text_occurrences.get(symbol, set()) if f != finding.file}
        if textual:
            unresolved.append("identifier appears elsewhere but is not resolved by the analyzer")

    if active:
        finding.reference = ReferenceKind.ACTIVE
        finding.reference_reasons = sorted(set(active))
    elif unresolved:
        finding.reference = ReferenceKind.UNRESOLVED
        finding.reference_reasons = sorted(set(unresolved))
    else:
        finding.reference = ReferenceKind.NONE
        finding.reference_reasons = [
            "no import edge or identifier reference found in the scanned scope"
        ]

    finding.coverage = coverage
    if facts.unresolved and coverage is Coverage.COMPLETE:
        finding.coverage = Coverage.PARTIAL
        finding.coverage_reasons.append(
            "unresolved import URI(s): "
            + ", ".join(sorted({u['uri'] for u in facts.unresolved})[:3])
        )
    if finding.impact is None:
        finding.impact = Impact.LOW

