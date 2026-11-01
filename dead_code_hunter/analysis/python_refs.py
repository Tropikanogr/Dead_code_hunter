"""Repository-wide Python reference analysis.

The goal is not to be clever: it is to make sure a symbol that *is* reachable
never reaches CANDIDATE.  Imports, decorator registrations, known dynamic
patterns and route tables become confirmed references (R3); any other textual
appearance becomes an unresolved reference (R7); common words produce no signal
but keep confidence LOW (spec X-3).
"""

from __future__ import annotations

import ast
import re
import tomllib
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
from .sources import SourceCache

ROUTE_ATTRS = {
    "get", "post", "put", "patch", "delete", "options", "head", "route",
    "websocket", "add_url_rule", "api_route", "add_api_route", "endpoint",
}
APP_RECEIVERS = {
    "app", "router", "bp", "blueprint", "api", "server", "routes", "api_router",
    "flask_app", "fastapi", "application",
}
SOCKET_RECEIVERS = {"sio", "socketio", "socket", "io", "sockets", "namespaces"}
SOCKET_ATTRS = {"on", "event", "on_event", "on_connect", "on_disconnect", "emit"}
RETAIN_DECORATORS = {
    "task", "shared_task", "periodic_task", "receiver", "fixture", "pytest.fixture",
    "pyqtslot", "slot", "property", "cached_property", "setter", "getter", "deleter",
    "validator", "root_validator", "field_validator", "command", "group",
    "on_event", "abstractmethod", "overload", "singledispatch", "singledispatchmethod",
    "contextmanager", "asynccontextmanager", "wraps", "app.task",
}
QT_SLOT_NAME = re.compile(r"^on_[A-Za-z0-9]+_[A-Za-z0-9_]+$")

_METHOD_KINDS = {"method", "attribute", "property", "unused attribute", "variable"}


@dataclass
class PythonFacts:
    root: Path
    project_roots: list[str] = field(default_factory=list)
    file_modules: dict[str, str] = field(default_factory=dict)
    module_files: dict[str, list[str]] = field(default_factory=dict)
    from_imports: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    attr_on_module: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    name_occurrences: dict[str, set[str]] = field(default_factory=dict)
    string_literals: dict[str, set[str]] = field(default_factory=dict)
    getattr_refs: set[str] = field(default_factory=set)
    event_handlers: set[str] = field(default_factory=set)
    connect_refs: set[str] = field(default_factory=set)
    table_refs: set[str] = field(default_factory=set)
    dynamic_modules: set[str] = field(default_factory=set)
    decorators: dict[str, set[str]] = field(default_factory=dict)
    all_exports: dict[str, set[str]] = field(default_factory=dict)
    main_modules: set[str] = field(default_factory=set)
    console_scripts: dict[str, tuple[str, str]] = field(default_factory=dict)
    distributed_projects: set[str] = field(default_factory=set)
    #: project -> repo-relative directories that hold its published top-level packages.
    package_roots: dict[str, set[str]] = field(default_factory=dict)
    #: project -> distribution name declared in pyproject.toml
    distribution_names: dict[str, str] = field(default_factory=dict)
    test_files: set[str] = field(default_factory=set)
    parse_errors: dict[str, str] = field(default_factory=dict)
    parse_error_files: list[str] = field(default_factory=list)

    # -- helpers -----------------------------------------------------------
    def module_of(self, file: str) -> str | None:
        return self.file_modules.get(file)

    def project_of(self, file: str) -> str:
        best, best_len = ".", -1
        for project in self.project_roots:
            if project in (".", ""):
                length = 0
            elif file == project or file.startswith(f"{project}/"):
                length = len(project)
            else:
                continue
            if length > best_len:
                best, best_len = project, length
        return best

    def reference_sources(self) -> str:
        return f"{len(self.file_modules)} python modules indexed"


def _module_name(rel: str, project: str) -> str:
    path = rel
    if project not in (".", "") and path.startswith(f"{project}/"):
        path = path[len(project) + 1:]
    parts = path.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts) or Path(rel).parent.name


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    names: list[str] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            names.append(".".join(reversed(parts)))
    return names


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    return any(
        isinstance(comparator, ast.Constant) and comparator.value == "__main__"
        for comparator in test.comparators
    )


def _safe_table_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Dict):
        found: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append(value.value)
            elif isinstance(value, (ast.Name, ast.Attribute)):
                found.append(ast.unparse(value))
        return found
    return []


def build_facts(root: Path, files: list[str], project_roots: list[str]) -> PythonFacts:
    cache = SourceCache(root)
    facts = PythonFacts(root=root, project_roots=sorted(project_roots, key=len, reverse=True))
    py_files = [f for f in files if f.endswith(".py")]
    for rel in py_files:
        project = facts.project_of(rel)
        module = _module_name(rel, project)
        facts.file_modules[rel] = module
        facts.module_files.setdefault(module, []).append(rel)
        name = Path(rel).name
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or "/tests/" in f"/{rel}":
            facts.test_files.add(rel)

    for rel in py_files:
        tree = cache.python_ast(rel)
        if tree is None:
            continue
        module = facts.file_modules[rel]
        module_aliases: dict[str, str] = {}
        module_exprs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    module_aliases[bound] = alias.name
                    if alias.asname:
                        module_exprs.add(alias.asname)
                    else:
                        parts = alias.name.split(".")
                        for index in range(1, len(parts) + 1):
                            module_exprs.add(".".join(parts[:index]))
            elif isinstance(node, ast.ImportFrom):
                source = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    facts.from_imports.setdefault((source, alias.name), set()).add(rel)
                    if source:
                        facts.from_imports.setdefault((source.split(".")[-1], alias.name), set()).add(rel)
        for node in ast.walk(tree):
            _collect_node(facts, rel, module_aliases, module_exprs, node)

    facts.parse_errors = dict(cache.parse_errors)
    facts.parse_error_files = sorted(cache.parse_errors)
    _load_project_metadata(root, project_roots, facts)
    return facts


def _collect_node(
    facts: PythonFacts,
    rel: str,
    module_aliases: dict[str, str],
    module_exprs: set[str],
    node: ast.AST,
) -> None:
    if isinstance(node, ast.Name):
        facts.name_occurrences.setdefault(node.id, set()).add(rel)
    elif isinstance(node, ast.Attribute):
        facts.name_occurrences.setdefault(node.attr, set()).add(rel)
        base = node.value
        if isinstance(base, ast.Name) and base.id in module_aliases:
            target = module_aliases[base.id]
            facts.attr_on_module.setdefault((target, node.attr), set()).add(rel)
            facts.attr_on_module.setdefault((target.split(".")[-1], node.attr), set()).add(rel)
        if module_exprs:
            try:
                base_expr = ast.unparse(base)
            except (ValueError, AttributeError):  # pragma: no cover - defensive
                base_expr = ""
            if base_expr in module_exprs:
                facts.attr_on_module.setdefault((base_expr, node.attr), set()).add(rel)
                facts.attr_on_module.setdefault((base_expr.split(".")[-1], node.attr), set()).add(rel)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        facts.string_literals.setdefault(node.value, set()).add(rel)
    elif isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            second = node.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                facts.getattr_refs.add(second.value)
        if isinstance(func, ast.Attribute) and func.attr == "import_module" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                facts.dynamic_modules.add(first.value)
                facts.dynamic_modules.add(first.value.split(".")[-1])
        if isinstance(func, ast.Attribute) and func.attr in {"connect", "connectSlotsByName"}:
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                _record_names(arg, facts.connect_refs)
    elif isinstance(node, ast.AugAssign):
        target = node.target
        if (isinstance(target, ast.Attribute) and "event" in target.attr.lower()) or (
            isinstance(target, ast.Name) and "event" in target.id.lower()
        ):
            _record_names(node.value, facts.event_handlers)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        for decorator in _decorator_names(node):
            facts.decorators.setdefault(node.name, set()).add(decorator)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                names = {
                    elt.value
                    for elt in getattr(node.value, "elts", [])
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
                if names:
                    facts.all_exports.setdefault(rel, set()).update(names)
            if isinstance(target, ast.Name) and target.id.isupper():
                for value in _safe_table_strings(node.value):
                    facts.table_refs.add(value.split(".")[-1])
    elif isinstance(node, ast.If) and _is_main_guard(node):
        facts.main_modules.add(rel)


def _record_names(node: ast.AST, target: set[str]) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            target.add(child.id)
        elif isinstance(child, ast.Attribute):
            target.add(child.attr)


def _load_project_metadata(root: Path, project_roots: list[str], facts: PythonFacts) -> None:
    for project in sorted(set(project_roots)):
        base = root if project in (".", "") else root / project
        pyproject = base / "pyproject.toml"
        data: dict = {}
        if pyproject.is_file():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                data = {}

        project_table = data.get("project")
        scripts: dict[str, str] = {}
        if isinstance(project_table, dict):
            facts.distributed_projects.add(project)
            declared_name = project_table.get("name")
            if isinstance(declared_name, str) and declared_name.strip():
                facts.distribution_names[project] = declared_name.strip()
            for key in ("scripts", "gui-scripts"):
                table = project_table.get(key)
                if isinstance(table, dict):
                    scripts.update({k: v for k, v in table.items() if isinstance(v, str)})
        poetry_table = data.get("tool", {})
        poetry_scripts = (
            poetry_table.get("poetry", {}).get("scripts")
            if isinstance(poetry_table, dict) and isinstance(poetry_table.get("poetry"), dict)
            else None
        )
        if isinstance(poetry_scripts, dict):
            scripts.update({k: v for k, v in poetry_scripts.items() if isinstance(v, str)})
            facts.distributed_projects.add(project)

        # A console script is an *entrypoint* of the package, not evidence that
        # the package is an application: a distributed library may ship a CLI.
        for _name, target in scripts.items():
            # ``module:func`` or ``module:obj.func`` (object attributes are common).
            if ":" not in target:
                continue
            module, func = target.split(":", 1)
            facts.console_scripts[_name] = (module.strip(), func.strip().split(".")[-1])

        roots = facts.package_roots.setdefault(project, set())
        for key in ("packages", "py-modules"):
            declared = project_table.get(key) if isinstance(project_table, dict) else None
            if isinstance(declared, list):
                for entry in declared:
                    if isinstance(entry, str) and entry:
                        roots.add(normalize_path(entry.replace(".", "/").replace("*", "").rstrip("/")))
        # Default layouts: flat (packages beside pyproject.toml) and src/.
        roots.update(_detect_package_roots(base, project, facts))


def _detect_package_roots(base: Path, project: str, facts: PythonFacts) -> set[str]:
    """Top-level importable package directories of a Python project."""
    found: set[str] = set()
    prefix = "" if project in (".", "") else project
    for file, _module in facts.file_modules.items():
        if prefix and not (file == prefix or file.startswith(f"{prefix}/")):
            continue
        relative = file[len(prefix):].lstrip("/") if prefix else file
        parts = relative.split("/")
        if len(parts) >= 2 and parts[0] == "src":
            if len(parts) >= 3 and parts[2] == "__init__.py":
                found.add(normalize_path(f"{prefix}/src/{parts[1]}" if prefix else f"src/{parts[1]}"))
            continue
        if len(parts) >= 2 and parts[1] == "__init__.py":
            found.add(normalize_path(f"{prefix}/{parts[0]}" if prefix else parts[0]))
    return found


def _is_init_public_surface(init_file: str, project: str, facts: PythonFacts) -> bool:
    """True when ``__init__.py`` is the public surface of a distributed package.

    Conservative by design.  The package counts as the published surface only
    when *all* of these hold (spec P3/P5, "when the public surface is
    uncertain, stay conservative"):

    1. the project declares ``[project]`` in its own ``pyproject.toml``;
    2. the ``__init__.py`` belongs to a detected top-level package directory;
    3. that directory's name is a token of the declared distribution name
       (``mylib14`` vs ``dch-mylib14``), i.e. the package really is what the
       distribution ships.

    A package whose name does not appear in the distribution name (a typical
    application, e.g. ``app`` shipped as ``myapp``) keeps the weaker
    INTERNAL_SHARED verdict: REVIEW_REQUIRED, never CANDIDATE, never PROTECTED.
    """
    if project not in facts.distributed_projects:
        return False
    package_dir = str(Path(init_file).parent).replace("\\", "/")
    if package_dir in ("", "."):
        return False
    roots = facts.package_roots.get(project) or set()
    if normalize_path(package_dir) not in {normalize_path(root) for root in roots}:
        return False
    distribution = facts.distribution_names.get(project)
    if not distribution:
        return False
    package_name = normalize_path(package_dir).rsplit("/", 1)[-1]

    def tokens(value: str) -> list[str]:
        return [part for part in re.split(r"[-_.]+", value.lower()) if part]

    normalise = lambda value: "-".join(tokens(value))  # noqa: E731
    package_name = package_name.lower()
    # Exact PEP 503-normalised match (acme_orders == acme-orders), or the
    # package is one of the distribution's name tokens (mylib14 in dch-mylib14).
    return normalise(package_name) == normalise(distribution) or package_name in tokens(
        distribution
    )


def _is_private_name(symbol: str | None) -> bool:
    """Leading-underscore names are private by convention (dunders excluded)."""
    if not symbol:
        return False
    if symbol.startswith("__") and symbol.endswith("__"):
        return False
    return symbol.startswith("_")


def _decorator_is_route(qualified: str) -> bool:
    parts = qualified.split(".")
    last = parts[-1]
    if len(parts) >= 2 and parts[-2].lower() in APP_RECEIVERS and last.lower() in ROUTE_ATTRS:
        return True
    return last.lower() in {"route", "add_url_rule"} or (
        last.lower() in ROUTE_ATTRS and len(parts) == 1
    )


def _decorator_is_socket(qualified: str) -> bool:
    parts = qualified.split(".")
    return (
        len(parts) >= 2
        and parts[-2].lower() in SOCKET_RECEIVERS
        and parts[-1].lower() in SOCKET_ATTRS
    )


def _decorator_is_retain(qualified: str) -> bool:
    lowered = qualified.lower()
    return lowered in RETAIN_DECORATORS or lowered.split(".")[-1] in RETAIN_DECORATORS


def base_confidence(finding: Finding, facts: PythonFacts) -> None:
    """Confidence per the spec's three levels."""
    del facts  # kept for a uniform per-language signature
    tool = finding.tool
    kind = finding.kind.lower()
    if is_common_word(finding.symbol):
        finding.confidence = Confidence.LOW
        finding.confidence_reasons = ["symbol is a common word; no signal is produced for it"]
        return
    if tool == "ruff" and kind in {"import", "local", "variable"}:
        finding.confidence = Confidence.HIGH
        finding.confidence_reasons = ["compiler-level fact local to this module (ruff)"]
        return
    if tool == "vulture":
        if kind in _METHOD_KINDS or kind in {"method", "attribute", "property"}:
            finding.confidence = Confidence.LOW
            finding.confidence_reasons = ["members/attributes are not proven unused by name analysis"]
            return
        finding.confidence = Confidence.MEDIUM
        finding.confidence_reasons = ["whole-program analysis with complete coverage (vulture)"]
        return
    if tool == "import-graph":
        finding.confidence = Confidence.MEDIUM
        finding.confidence_reasons = ["file unreachable in the static import graph"]
        return
    finding.confidence = Confidence.MEDIUM
    finding.confidence_reasons = [f"default confidence for tool {tool}"]


def enrich(finding: Finding, facts: PythonFacts, coverage: Coverage, project: str) -> None:
    symbol = finding.symbol
    module = facts.module_of(finding.file)

    base_confidence(finding, facts)

    # ---- contract -------------------------------------------------------
    finding.contract = Contract.NONE
    decorators = facts.decorators.get(symbol or "", set()) if symbol else set()
    if any(_decorator_is_route(d) for d in decorators):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append(
            f"HTTP route registration: @{sorted(d for d in decorators if _decorator_is_route(d))[0]}"
        )
    elif any(_decorator_is_socket(d) for d in decorators):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("Socket.IO event registration")
    elif symbol and any(
        func == symbol and mod in {(module or ""), (module or "").split(".")[-1]}
        for mod, func in facts.console_scripts.values()
    ):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("console script entrypoint ([project.scripts])")
    elif finding.file in facts.main_modules:
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("module is a CLI entrypoint (if __name__ == '__main__')")
    elif finding.file.endswith("__main__.py"):
        finding.contract = Contract.PUBLIC
        finding.contract_reasons.append("module is executed as a package entrypoint (__main__.py)")

    exported = facts.all_exports.get(finding.file, set())
    init_surface = finding.file.endswith("__init__.py")
    if symbol and symbol in exported and finding.contract is Contract.NONE:
        # A name published from a top-level package of a distributed library is
        # part of its public API, whether or not __all__ is used to list it.
        if _is_init_public_surface(finding.file, project, facts) and not _is_private_name(symbol):
            finding.contract = Contract.PUBLIC
            finding.contract_reasons.append(
                f"published in __all__ of the public package {finding.file}"
            )
        else:
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append(f"symbol is listed in __all__ of {finding.file}")

    if init_surface and finding.contract is Contract.NONE:
        if _is_private_name(symbol):
            # Private by convention: re-exporting it does not publish it, but it
            # is still a shared internal surface -> conservative INTERNAL_SHARED.
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append(
                f"private name re-exported from {finding.file}; not part of the public surface"
            )
        elif _is_init_public_surface(finding.file, project, facts):
            finding.contract = Contract.PUBLIC
            finding.contract_reasons.append(
                "name re-exported from a top-level package of a distributed library"
            )
        else:
            finding.contract = Contract.INTERNAL_SHARED
            finding.contract_reasons.append(f"re-export in {finding.file}")

    # ---- references -----------------------------------------------------
    active: list[str] = []
    unresolved: list[str] = []
    if symbol:
        importers = {
            f for f in facts.from_imports.get((module or "", symbol), set()) if f != finding.file
        }
        short_module = (module or "").split(".")[-1]
        importers |= {
            f for f in facts.from_imports.get((short_module, symbol), set()) if f != finding.file
        }
        if importers:
            active.append(f"imported by {', '.join(sorted(importers)[:3])}")
        attribute_users = {
            f for f in facts.attr_on_module.get((module or "", symbol), set()) if f != finding.file
        }
        attribute_users |= {
            f for f in facts.attr_on_module.get((short_module, symbol), set()) if f != finding.file
        }
        if attribute_users:
            active.append(f"module attribute accessed in {', '.join(sorted(attribute_users)[:3])}")
        if symbol in facts.getattr_refs:
            active.append("name resolved dynamically via getattr()")
        if symbol in facts.event_handlers:
            active.append("registered as an event handler (X.event += handler)")
        if symbol in facts.connect_refs:
            active.append("connected as a Qt signal slot")
        if symbol in facts.table_refs:
            active.append("referenced from a static route/handler table")
        if not active and any(_decorator_is_retain(d) for d in decorators):
            active.append(
                "registration decorator: "
                + ", ".join(sorted(d for d in decorators if _decorator_is_retain(d))[:2])
            )
        if not active and QT_SLOT_NAME.match(symbol):
            active.append("Qt auto-connect slot naming (on_<object>_<signal>)")
        if not active and symbol.startswith("__") and symbol.endswith("__"):
            active.append("dunder method invoked by the runtime")
        if module and module in facts.dynamic_modules:
            active.append(f"module '{module}' is imported dynamically")
        dotted = f"{module}.{symbol}" if module else symbol
        string_users = {f for f in facts.string_literals.get(dotted, set()) if f != finding.file}
        if string_users:
            active.append(f"referenced by name in {', '.join(sorted(string_users)[:3])}")

        if not active:
            if finding.kind.lower() in {"import", "unused import"}:
                textual = {f for f in facts.string_literals.get(symbol, set()) if f != finding.file}
                attribute_only = {f for f in facts.name_occurrences.get(symbol, set()) if f != finding.file}
                if textual:
                    unresolved.append(
                        f"name appears as text in {', '.join(sorted(textual)[:3])}"
                    )
                elif attribute_only and not is_common_word(symbol):
                    unresolved.append(
                        "name appears in other modules but the reference is not statically resolved"
                    )
            else:
                appearances = {
                    f for f in facts.name_occurrences.get(symbol, set()) if f != finding.file
                }
                textual = {f for f in facts.string_literals.get(symbol, set()) if f != finding.file}
                if is_common_word(symbol):
                    appearances, textual = set(), set()
                if appearances:
                    unresolved.append(
                        f"identifier appears in {', '.join(sorted(appearances)[:3])} without a resolvable binding"
                    )
                if textual:
                    unresolved.append("identifier appears in string literals")

    if active:
        finding.reference = ReferenceKind.ACTIVE
        finding.reference_reasons = sorted(set(active))
    elif unresolved:
        finding.reference = ReferenceKind.UNRESOLVED
        finding.reference_reasons = sorted(set(unresolved))
    else:
        finding.reference = ReferenceKind.NONE
        finding.reference_reasons = ["no reference found in the scanned scope"]

    finding.coverage = coverage
    if facts.parse_error_files and coverage is Coverage.COMPLETE:
        finding.coverage = Coverage.PARTIAL
        finding.coverage_reasons.append(
            "reference index is incomplete: unparsable file(s) "
            + ", ".join(facts.parse_error_files[:3])
        )
    if finding.impact is None:
        finding.impact = Impact.LOW
