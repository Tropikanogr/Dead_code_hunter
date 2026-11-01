"""Adapter registry.

Adding an analyzer means adding an adapter class here; the classifier and the
report writer stay unchanged (spec X-6 / AD-1).
"""

from __future__ import annotations

from .base import Adapter
from .dart_tools import DartAnalyzeAdapter
from .knip_adapter import KnipAdapter
from .python_tools import RuffAdapter, VultureAdapter

PRODUCTION_ADAPTERS: tuple[type[Adapter], ...] = (
    RuffAdapter,
    VultureAdapter,
    KnipAdapter,
    DartAnalyzeAdapter,
)

BY_LANG: dict[str, list[type[Adapter]]] = {
    "python": [RuffAdapter, VultureAdapter],
    "js": [KnipAdapter],
    "dart": [DartAnalyzeAdapter],
}


def adapters_for(lang: str, extra: list[type[Adapter]] | None = None) -> list[type[Adapter]]:
    return list(BY_LANG.get(lang, [])) + [a for a in (extra or []) if a.lang == lang]
