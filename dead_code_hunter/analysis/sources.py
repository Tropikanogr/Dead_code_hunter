"""Cached, secret-aware source access shared by the reference analyses."""

from __future__ import annotations

import ast
from pathlib import Path

from ..models import normalize_path
from ..safety import is_secret_path


class SourceCache:
    """Reads repository files at most once and never opens secret files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._text: dict[str, str | None] = {}
        self._ast: dict[str, ast.Module | None] = {}
        self.parse_errors: dict[str, str] = {}
        self.read_errors: dict[str, str] = {}
        self.skipped_secrets = 0

    def read(self, rel: str) -> str | None:
        rel = normalize_path(rel)
        if rel in self._text:
            return self._text[rel]
        if is_secret_path(Path(rel).name):
            self.skipped_secrets += 1
            self._text[rel] = None
            return None
        try:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.read_errors[rel] = str(exc)
            self._text[rel] = None
            return None
        self._text[rel] = text
        return text

    def python_ast(self, rel: str) -> ast.Module | None:
        rel = normalize_path(rel)
        if rel in self._ast:
            return self._ast[rel]
        text = self.read(rel)
        if text is None:
            self._ast[rel] = None
            return None
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as exc:
            self.parse_errors[rel] = f"{exc.msg} (line {exc.lineno})"
            self._ast[rel] = None
            return None
        self._ast[rel] = tree
        return tree
