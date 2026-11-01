"""Small path predicates shared by the language analyses."""

from __future__ import annotations

TEST_DIRECTORIES = frozenset(
    {"test", "tests", "integration_test", "test_driver", "__tests__", "e2e", "cypress"}
)


def is_test_file(rel: str) -> bool:
    """True when a repo-relative path belongs to test code."""
    lowered = rel.replace("\\", "/").lower()
    segments = lowered.split("/")
    if any(segment in TEST_DIRECTORIES for segment in segments[:-1]):
        return True
    name = segments[-1]
    return (
        ".test." in name
        or ".spec." in name
        or name.startswith("test_")
        or name.endswith(("_test.dart", "_test.py", "_test.js", "_test.ts", "_test.tsx", "_test.jsx"))
    )
