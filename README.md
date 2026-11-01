# Dead Code Hunter — v1.1 / Milestone M1 (Scanner Correctness)

Read-only dead-code scanner for **Python**, **TypeScript/JavaScript** and **Dart/Flutter**.

> The Hunter never presents ignorance as certainty. A `CANDIDATE` is a strong
> candidate for human review — never proof that code is safe to delete.

This repository contains **M1 only**: the modular CLI scanner, analyzers,
normalized findings, deterministic classification, coverage reporting, ignore
rules and JSON/Markdown reports. There is no MCP server, no agent, no cleaner,
no snapshot engine, no approval workflow and no dependency provisioning (DP-1…DP-4).

## Quick start

```powershell
# Scan a trusted local checkout; reports are written outside the repository
python -m dead_code_hunter scan --repo C:\path\to\repo

# Machine-readable output on stdout
python -m dead_code_hunter scan --repo . --no-write --format json

# Where the reports went
python -m dead_code_hunter scan --repo . --print-report-path
```

Reports are written to `%LOCALAPPDATA%\dead-code-hunter\<repo-id>\<scan_id>\report.json|report.md`
(override the base directory with `--reports-dir` or `DEAD_CODE_HUNTER_REPORTS`).
A base directory that resolves inside the scanned repository is refused.

Exit codes: `0` ok · `1` error or (`--strict`) a failed analyzer · `2` the
repository changed during the scan.

## What it needs

Only tools that are already installed locally. Nothing is downloaded and
nothing is installed.

| Language | Analyzer | Lookup order |
| --- | --- | --- |
| Python | `ruff` (`F401`, `F841`) | project `.venv`, then `PATH` |
| Python | `vulture` | project `.venv`, then `PATH` |
| JS/TS | `knip` | `node_modules/.bin` only — never `npx`, never a global install |
| Dart | `dart analyze --format=machine` | `PATH` (or the Flutter SDK shim) |
| Dart | import graph (built in) | pure Python, always available |

A missing or failing analyzer becomes `coverage FAILED/PARTIAL` with the reason —
it is never reported as success.

## Architecture

```
dead_code_hunter/
  cli.py             CLI and exit codes
  scanner.py         orchestration: discovery → analyzers → evidence → classify → report
  discovery.py       repo walk, project/package detection (PY-4, JS-4)
  adapters/          one adapter per analyzer, common contract (AD-1 / X-6)
    python_tools.py    ruff, vulture
    knip_adapter.py    knip + the DP-5 completeness gate
    dart_tools.py      dart analyze + the import graph
  parsers.py         pure parsers for ruff/vulture/knip/dart output (success and failure)
  analysis/          repository-wide reference analysis
    python_refs.py     imports, decorators, getattr/importlib, event handlers, __all__, __init__
    js_refs.py         entry files, barrels, dynamic imports, routes, sockets, framework conventions
    dart_refs.py       conditional imports, part/export/deferred, package: resolution, orphan files
    entrypoints.py     Dockerfile / Procfile / shell / pm2 launch points
  classify.py        R1–R8, analyzer-agnostic
  coverage.py        coverage aggregation
  dedupe.py          deterministic merge of corroborating findings
  config.py          .dead-code/config.toml (impact policy) and .dead-code/ignore
  safety.py          secret exclusion, environment scrubbing
  tools.py           allowlisted subprocess runner (shell=False, timeouts, output limits)
  gitstate.py        git status before/after
  report.py          JSON + Markdown writers
```

The classification engine knows nothing about analyzers: it reads only the four
evidence axes and the reference outcome of a finding. Any adapter that returns
`AnalyzerResult` (findings + coverage + reason) plugs in without touching
`classify.py`.

## Classification (R1–R8, first match wins)

| Rule | Condition | Recommendation |
| --- | --- | --- |
| R1 | category ≠ `dead_code` (e.g. knip `unlisted`, `unresolved`, `binaries`) | `INFO` |
| R2 | contract sensitivity `PUBLIC` | `PROTECTED` |
| R3 | confirmed active reference or recognised registration | `RETAIN` |
| R4 | coverage of the finding's analyzer ≠ `COMPLETE` | `REVIEW_REQUIRED` |
| R5 | contract `INTERNAL_SHARED` without a confirmed reference | `REVIEW_REQUIRED` |
| R6 | impact `HIGH`/`CRITICAL` (billing, payment, auth, driver, shift, mydata, …) | `REVIEW_REQUIRED` |
| R7 | confidence `LOW` or an unresolved textual reference | `REVIEW_REQUIRED` |
| R8 | private, unreferenced, complete coverage | `CANDIDATE` |

## Coverage

`COMPLETE` means the declared scope and tool ran fully. `PARTIAL` means the
analysis ran but has a named gap (failing file, unresolved import, missing
`node_modules`, uncovered entrypoint, DP-5 condition). `FAILED` means the tool
did not produce a trustworthy result. `NOT_APPLICABLE` means the analyzer has
nothing to do in this project. Every gap is named in `coverage[].reason`, and
`inherited: true` marks a project whose coverage comes from an ancestor run.

## Repository configuration (read-only)

`.dead-code/config.toml`

```toml
[impact]
high = ["billing", "invoice"]      # keyword match on path, symbol and project
critical = ["payment", "auth"]
medium = ["reporting"]

[scan]
timeout_seconds = 180
max_projects = 64
```

`.dead-code/ignore`

```
path/glob              # ignore every finding in matching files
path/glob::symbol      # ignore one symbol
```

Ignored findings are not deleted from the record: they appear in `ignored`
together with the matching rule.

## Safety

* `.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*.json`, `secrets.*`,
  `.npmrc`, `.pypirc`, `.netrc` are never opened (not for scanning, not for
  indexing, not for the reference analyses).
* Subprocesses run with an allowlisted executable set, `shell=False`, a scrubbed
  environment (`*TOKEN*`, `*SECRET*`, `*KEY*`, `*PASSWORD*`, `*_URL*`, `AWS_*`,
  `IBKR*` removed), timeouts and output limits.
* No `npx --yes`, no downloads, no `pip/npm/pub install`, no writes inside the
  repository. `git status` is captured before and after; a difference is
  reported and makes the exit code non-zero.
* `isolation` in every report states the truth: M1 has **no** OS sandbox, **no**
  network block and **no** snapshot. Only repositories you trust should be scanned.

## Tests

```powershell
python -m pytest tests -q
```

348 tests. Fixtures live in `tests/fixtures_repo.py`; each one declares the raw
output the real tool produces, so the tests check parsing and judgement instead
of self-consistency. Analyzer stubs (`tests/stubs/fake_tool.py`) are used only
where the tool is not installed on the host; the tests that use real `ruff` and
real `dart analyze` skip when those tools are absent.

## M1 fixtures

Python: F-PY-01…F-PY-14 · JS/TS: F-JS-01…F-JS-12 · Dart: F-DT-01…F-DT-07 ·
cross-cutting: F-X-01, F-X-02a, F-X-04, F-X-05.

## Known limitations (M1)

* No snapshot/sandbox: analyzers run on the real checkout. `dart analyze` may
  create `.dart_tool/`; the git-integrity check reports it.
* `vulture` and `knip` are not installed on this development host, so their
  integration is verified against faithful stubs, not the live binaries.
* Reference analysis for Python and JS is syntactic (AST/regex) plus known
  dynamic patterns. Unknown dynamic wiring can only produce `REVIEW_REQUIRED`
  or `RETAIN`, never `CANDIDATE` — but it can also hide real dead code.
* JS path aliases (`tsconfig` `paths`) are not resolved by the scanner's own
  reference index; `knip` resolves them, and the reference index only adds
  retention.
* Dart `package:` URIs to third-party packages are not "unresolved": only
  repository-internal URIs count as analysis gaps.

### Recording decision: `__all__` vs. published surface

The specification assigns `__all__` names and `__init__.py` re-exports the
contract `INTERNAL_SHARED` (PY-1, F-PY-04 → `REVIEW_REQUIRED`) while also saying
that symbols exposed through a published package's entrypoints are `PUBLIC`
(→ `PROTECTED`, F-PY-14). The implementation resolves this by treating
`__all__` entries as `INTERNAL_SHARED`, and `__init__.py` re-exports *without*
`__all__` as `PUBLIC` when the package is a distributed library without
application entrypoints. Both resolutions are conservative (never `CANDIDATE`).
