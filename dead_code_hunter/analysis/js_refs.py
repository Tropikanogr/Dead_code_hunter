"""Repository-wide JS/TS reference analysis.

Knip provides the whole-program view; this module is the independent safety net
required by JS-5/DP-5:

* entry files are computed by the scanner itself (package.json fields plus
  Dockerfile / Procfile / shell / pm2 launches), never trusted from knip config;
* route and Socket.IO registrations are public contracts;
* any import edge proves a file is reachable;
* framework file conventions and dynamic imports are confirmed references.
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
from .entrypoints import Entrypoint
from .pathfacts import is_test_file
from .sources import SourceCache

EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")

_IMPORT_FROM = re.compile(
    r"(?:^|[\s;}])import\s+(?P<clause>[^'\"]*?)\s*from\s*['\"](?P<spec>[^'\"]+)['\"]",
    re.MULTILINE,
)
_IMPORT_BARE = re.compile(r"(?:^|[\s;}])import\s*['\"](?P<spec>[^'\"]+)['\"]", re.MULTILINE)
_REQUIRE = re.compile(r"require\s*\(\s*['\"](?P<spec>[^'\"]+)['\"]\s*\)")
_EXPORT_FROM = re.compile(
    r"export\s+(?P<clause>[\*{}\s\w,$]*?)\s*from\s*['\"](?P<spec>[^'\"]+)['\"]", re.MULTILINE
)
_DYNAMIC_IMPORT = re.compile(r"import\s*\(\s*(?P<arg>[^)]*?)\s*\)")
_EXPORT_DECL = re.compile(
    r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|interface|type|enum)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_EXPORT_LIST = re.compile(r"export\s*\{(?P<names>[^}]*)\}")
_NAMED_IMPORT = re.compile(r"^\{?\s*(?P<names>[\w$,\s]+?)\s*\}?$")
_ROUTE_CALL = re.compile(
    r"\b(?P<recv>app|router|server|api|routes)\s*\.\s*(?P<method>get|post|put|patch|delete|options|head|all|use|route)\s*\("
)
_SOCKET_CALL = re.compile(r"\b(?P<recv>socket|io|sio|socketio)\s*\.\s*(?P<method>on|once|emit)\s*\(")
_NEXT_CONVENTION = re.compile(
    r"^(?:app/(?:.+/)?(?:page|layout|route|loading|error|template|not-found|default)\.[cm]?[jt]sx?$"
    r"|pages/(?:.+/)?[^/]+\.[cm]?[jt]sx?$"
    r"|middleware\.[cm]?[jt]s$"
    r"|app/routes/.+\.[cm]?[jt]sx?$)"
)
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass
class JsFacts:
    root: Path
    files: list[str] = field(default_factory=list)
    imported_names: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    namespace_imports: dict[str, set[str]] = field(default_factory=dict)
    reexports: dict[str, dict[str, str]] = field(default_factory=dict)
    wildcard_reexports: dict[str, set[str]] = field(default_factory=dict)
    dynamic_targets: dict[str, set[str]] = field(default_factory=dict)
    unresolved_dynamic: list[dict[str, str | int]] = field(default_factory=list)
    text_occurrences: dict[str, set[str]] = field(default_factory=dict)
    route_files: dict[str, list[str]] = field(default_factory=dict)
    socket_files: dict[str, list[str]] = field(default_factory=dict)
    next_convention_files: set[str] = field(default_factory=set)
    entry_files: dict[str, str] = field(default_factory=dict)
    entrypoints: list[Entrypoint] = field(default_factory=list)
    package_for_file: dict[str, str] = field(default_factory=dict)
    package_private: dict[str, bool] = field(default_factory=dict)
    package_jsons: list[str] = field(default_factory=list)

    def package_of(self, file: str) -> str | None:
        return self.package_for_file.get(file)

    def is_published(self, file: str) -> bool:
        package = self.package_of(file)
        if package is None:
            return True  # undeterminable -> treat as public (spec contract rule)
        return not self.package_private.get(package, False)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", " ", text)


def _resolve_specifier(spec: str, from_file: str, fileset: set[str]) -> str | None:
    if not spec.startswith((".", "/")):
        return None
    base = normalize_path(posixpath.normpath(posixpath.join(posixpath.dirname(from_file), spec)))
    if base in fileset:
        return base
    for ext in EXTENSIONS:
        if base + ext in fileset:
            return base + ext
    for ext in EXTENSIONS:
        candidate = normalize_path(f"{base}/index{ext}")
        if candidate in fileset:
            return candidate
    if base.endswith("/"):
        for ext in EXTENSIONS:
            if base + f"index{ext}" in fileset:
                return base + f"index{ext}"
    return None


def _parse_import_clause(clause: str) -> tuple[list[str], str | None, bool]:
    """Return (named imports, default import, namespace import)."""
    clause = clause.strip()
    default: str | None = None
    namespace = False
    named: list[str] = []
    if not clause:
        return named, default, namespace
    if clause.startswith("{"):
        inner = clause.strip("{} ")
        for part in inner.split(","):
            piece = part.strip()
            if not piece:
                continue
            if piece.startswith("type "):
                piece = piece[len("type "):]
            name = piece.split(" as ")[0].strip()
            if name:
                named.append(name)
        return named, default, namespace
    match = re.match(r"^(?P<default>[\w$]+)\s*(?:,\s*(?P<rest>.*))?$", clause, re.DOTALL)
    if match:
        default = match.group("default")
        rest = (match.group("rest") or "").strip()
        if rest.startswith("*"):
            namespace = True
        elif rest.startswith("{"):
            for part in rest.strip("{} ").split(","):
                piece = part.strip()
                if piece:
                    named.append(piece.split(" as ")[0].strip())
    elif clause.startswith("*"):
        namespace = True
    return named, default, namespace


def build_facts(
    root: Path,
    files: list[str],
    packages: list,  # list[discovery.PackageJson]
    entrypoints: list[Entrypoint],
) -> JsFacts:
    facts = JsFacts(root=root, entrypoints=entrypoints)
    js_files = [f for f in files if f.endswith(EXTENSIONS)]
    facts.files = js_files
    fileset = set(files)
    cache = SourceCache(root)

    package_dirs = sorted(
        ((pkg.dir, pkg) for pkg in packages), key=lambda item: len(item[0]), reverse=True
    )
    for rel in js_files:
        owner = None
        for directory, pkg in package_dirs:
            if directory == "." or rel.startswith(f"{directory}/"):
                owner = pkg.path
                break
        if owner is not None:
            facts.package_for_file[rel] = owner
    for pkg in packages:
        facts.package_jsons.append(pkg.path)
        facts.package_private[pkg.path] = pkg.private

    for rel in js_files:
        text = cache.read(rel)
        if text is None:
            continue
        source = _strip_comments(text)
        _scan_imports(facts, rel, source, fileset)
        if _ROUTE_CALL.search(source):
            facts.route_files[rel] = sorted(
                {m.group("method") for m in _ROUTE_CALL.finditer(source)}
            )
        if _SOCKET_CALL.search(source):
            facts.socket_files[rel] = sorted(
                {m.group("method") for m in _SOCKET_CALL.finditer(source)}
            )
        if _NEXT_CONVENTION.match(rel.replace("\\", "/")):
            facts.next_convention_files.add(rel)
        for match in _IDENT.finditer(source):
            facts.text_occurrences.setdefault(match.group(0), set()).add(rel)

    for pkg in packages:
        _collect_package_entries(facts, pkg, fileset)
    for entry in entrypoints:
        if entry.path:
            facts.entry_files.setdefault(entry.path, f"{entry.kind}:{entry.source}")
    return facts


def _scan_imports(facts: JsFacts, rel: str, source: str, fileset: set[str]) -> None:
    for match in _IMPORT_FROM.finditer(source):
        spec = match.group("spec")
        target = _resolve_specifier(spec, rel, fileset)
        if target is None:
            continue
        named, default, namespace = _parse_import_clause(match.group("clause"))
        names = set(named)
        if default:
            names.add("default")
        for name in names:
            facts.imported_names.setdefault(target, {}).setdefault(name, set()).add(rel)
        if namespace:
            facts.namespace_imports.setdefault(target, set()).add(rel)
    for match in _IMPORT_BARE.finditer(source):
        _resolve_specifier(match.group("spec"), rel, fileset)  # bare import: reachability only
    for match in _REQUIRE.finditer(source):
        target = _resolve_specifier(match.group("spec"), rel, fileset)
        if target is not None:
            facts.imported_names.setdefault(target, {}).setdefault("default", set()).add(rel)
    for match in _EXPORT_FROM.finditer(source):
        target = _resolve_specifier(match.group("spec"), rel, fileset)
        if target is None:
            continue
        clause = match.group("clause").strip()
        if clause.startswith("*"):
            facts.wildcard_reexports.setdefault(rel, set()).add(target)
        elif clause.startswith("{"):
            for part in clause.strip("{} ").split(","):
                piece = part.strip()
                if not piece:
                    continue
                exported_name = piece.split(" as ")[-1].strip()
                facts.reexports.setdefault(rel, {})[exported_name] = target
    for match in _DYNAMIC_IMPORT.finditer(source):
        argument = match.group("arg").strip()
        if argument.startswith(("'", '"', "`")):
            literal = argument.strip("'\"`")
            target = _resolve_specifier(literal, rel, fileset)
            if target is not None:
                facts.dynamic_targets.setdefault(target, set()).add(rel)
            else:
                facts.unresolved_dynamic.append(
                    {"file": rel, "specifier": literal, "reason": "dynamic import not resolved"}
                )
        else:
            line = source[: match.start()].count("\n") + 1
            facts.unresolved_dynamic.append(
                {"file": rel, "specifier": argument[:80], "line": line,
                 "reason": "non-literal dynamic import"}
            )
    for match in _EXPORT_DECL.finditer(source):
        facts.text_occurrences.setdefault(match.group("name"), set()).add(rel)


def _collect_package_entries(facts: JsFacts, pkg, fileset: set[str]) -> None:
    data = pkg.data

    def add(value: object, reason: str) -> None:
        if isinstance(value, str):
            resolved = _resolve_specifier(f"./{value}", normalize_path(f"{pkg.dir}/package.json"), fileset)
            if resolved is None:
                resolved = _resolve_specifier(f"./{value}", normalize_path(f"{pkg.path}"), fileset)
            if resolved:
                facts.entry_files.setdefault(resolved, reason)
        elif isinstance(value, list):
            for item in value:
                add(item, reason)
        elif isinstance(value, dict):
            for item in value.values():
                add(item, reason)

    for field_name in ("main", "module", "browser", "types", "typings", "source"):
        add(data.get(field_name), f"package.json:{field_name}")
    add(data.get("bin"), "package.json:bin")
    add(data.get("exports"), "package.json:exports")
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for value in scripts.values():
            if isinstance(value, str):
                for token in re.findall(r"[\w./\\-]+", value):
                    if token.endswith(EXTENSIONS):
                        resolved = _resolve_specifier(
                            f"./{token}", normalize_path(f"{pkg.dir}/package.json"), fileset
                        )
                        if resolved:
                            facts.entry_files.setdefault(resolved, "package.json:scripts")


def base_confidence(finding: Finding) -> None:
    if is_common_word(finding.symbol):
        finding.confidence = Confidence.LOW
        finding.confidence_reasons = ["symbol is a common word; no signal is produced for it"]
        return
    kind = finding.kind.lower()
    if kind in {"member", "classmember", "enummember", "property", "method", "attribute"}:
        finding.confidence = Confidence.LOW
        finding.confidence_reasons = ["members are not proven unused by name analysis"]
        return
    if kind in {"dependency", "devdependency", "unlisted", "unresolved", "binary", "catalog", "duplicate"}:
        finding.confidence = Confidence.MEDIUM
        finding.confidence_reasons = ["dependency report item, not a code symbol"]
        return
    finding.confidence = Confidence.MEDIUM
    finding.confidence_reasons = ["whole-program export/file analysis (knip)"]


def _consumers(facts: JsFacts, file: str, symbol: str | None) -> set[str]:
    """Files that import ``symbol`` from ``file`` (directly or through barrels)."""
    consumers: set[str] = set()
    if symbol:
        consumers |= facts.imported_names.get(file, {}).get(symbol, set())
    else:
        for names in facts.imported_names.get(file, {}).values():
            consumers |= names
    consumers |= facts.dynamic_targets.get(file, set())
    consumers |= facts.namespace_imports.get(file, set())
    return {c for c in consumers if c != file}


def _barrel_consumers(facts: JsFacts, file: str, symbol: str | None) -> set[str]:
    consumers: set[str] = set()
    for barrel, mapping in facts.reexports.items():
        if mapping.get(symbol or "") == file:
            consumers |= facts.imported_names.get(barrel, {}).get(symbol or "", set())
    for barrel, targets in facts.wildcard_reexports.items():
        if file in targets and symbol:
            consumers |= facts.imported_names.get(barrel, {}).get(symbol, set())
            consumers |= facts.namespace_imports.get(barrel, set())
    return {c for c in consumers if c != file}


def enrich(finding: Finding, facts: JsFacts, coverage: Coverage) -> None:
    symbol = finding.symbol
    base_confidence(finding)

    # ---- contract -------------------------------------------------------
    finding.contract = Contract.NONE
    entry_reason = facts.entry_files.get(finding.file)
    if entry_reason:
        if facts.is_published(finding.file):
            finding.contract = Contract.PUBLIC
            finding.contract_reasons.append(f"entry file of a published package ({entry_reason})")
        else:
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append(
                f"entry file of a private package, still consumable via the monorepo ({entry_reason})"
            )
    if finding.contract is Contract.NONE and facts.route_files.get(finding.file):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append(
            "HTTP route registration (."
            + "/.".join(facts.route_files[finding.file][:3])
            + ")"
        )
    if finding.contract is Contract.NONE and facts.socket_files.get(finding.file):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("Socket.IO event registration")

    if finding.contract is Contract.NONE:
        basename = Path(finding.file).name
        if basename.startswith("index.") and "/" in f"/{finding.file}":
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append(f"barrel re-export module {finding.file}")

    # ---- references -----------------------------------------------------
    active: list[str] = []
    unresolved: list[str] = []

    consumers = _consumers(facts, finding.file, symbol) | _barrel_consumers(facts, finding.file, symbol)
    production_consumers = {c for c in consumers if not is_test_file(c)}
    if production_consumers:
        active.append(f"imported by {', '.join(sorted(production_consumers)[:3])}")
    elif consumers:
        finding.confidence = Confidence.LOW
        finding.confidence_reasons = ["referenced only from tests"]
        finding.signals = sorted(set(finding.signals) | {"reachable-only-from-tests"})
    if finding.file in facts.next_convention_files:
        active.append("framework file convention (Next/Remix style route file)")
    if finding.file in facts.dynamic_targets:
        active.append("loaded through a dynamic import()")

    if not active and symbol:
        others = {f for f in facts.text_occurrences.get(symbol, set()) if f != finding.file}
        if others and not is_common_word(symbol):
            unresolved.append(
                f"identifier appears in {', '.join(sorted(others)[:3])} without a resolvable binding"
            )

    if active:
        finding.reference = ReferenceKind.ACTIVE
        finding.reference_reasons = sorted(set(active))
    elif unresolved:
        finding.reference = ReferenceKind.UNRESOLVED
        finding.reference_reasons = sorted(set(unresolved))
    else:
        finding.reference = ReferenceKind.NONE
        finding.reference_reasons = ["no import or reference found in the scanned scope"]

    finding.coverage = coverage
    if finding.impact is None:
        finding.impact = Impact.LOW

