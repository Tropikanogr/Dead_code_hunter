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

# Limit the reported findings to a subtree (analysis stays repository-wide)
python -m dead_code_hunter scan --repo . --scope src/api

# Fail on any incomplete analysis (PARTIAL, FAILED, skipped or unmatched scope)
python -m dead_code_hunter scan --repo . --strict

# Where the reports went
python -m dead_code_hunter scan --repo . --print-report-path
```

### What `--scope` means

Scope selects **what is reported** and **which projects run** — it never
narrows the reference index:

* the reference index always covers the whole repository, so a symbol used
  outside the scope is still `RETAIN` (spec R3);
* a scope nested inside a project runs the *parent* project, so sibling
  modules remain visible;
* a scope that matches no file, or points outside the repository, is an
  explicit coverage `FAILED` entry plus a `--strict` failure — never a
  silently clean scan.

Reports are written to `%LOCALAPPDATA%\dead-code-hunter\<repo-id>\<scan_id>\report.json|report.md`
(override the base directory with `--reports-dir` or `DEAD_CODE_HUNTER_REPORTS`).
A base directory that resolves inside the scanned repository is refused.

Exit codes: `0` ok · `1` error or (`--strict`) an incomplete analysis · `2` the
repository changed during the scan.

`--strict` fails on `PARTIAL` as well as `FAILED`, on a project that was never
analysed, on a truncated file index, on a project dropped by `max_projects`, and
on a scope that matched nothing.

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
`node_modules`, uncovered entrypoint, DP-5 condition, truncated file index).
`FAILED` means the tool did not produce a trustworthy result.
`NOT_APPLICABLE` means the analyzer has nothing to do in this project. Every gap
is named in `coverage[].reason`, and `inherited: true` marks a project whose
coverage comes from an ancestor run.

Two discovery-level gaps get their own rows instead of being folded into a
tool's verdict: projects dropped by `max_projects` appear as `FAILED` entries
with `tool: "<skipped>"`, and a scope that resolved to nothing appears as
`FAILED` with `tool: "<scope>"`.

Merging corroborating findings never upgrades coverage: if any contributing
analyzer was `PARTIAL` or `FAILED`, the merged finding inherits the weakest
verdict (a clean tool cannot vouch for an unclean one).

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
max_files = 200000        # truncation downgrades coverage to PARTIAL
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

Fixtures live in `tests/fixtures_repo.py`; each one declares the raw output the
real tool produces, so the tests check parsing and judgement instead of
self-consistency. Analyzer stubs (`tests/stubs/fake_tool.py`) are used only
where the tool is not installed on the host.

`tests/test_integration_real_tools.py` runs **real** `ruff`, `vulture` and
`dart analyze` on fixtures and **skips** (never passes) when a tool is absent.
`knip` is verified to stay unrun without a local `node_modules/.bin/knip`.

## M1 fixtures

Python: F-PY-01…F-PY-14 · JS/TS: F-JS-01…F-JS-12 · Dart: F-DT-01…F-DT-07 ·
cross-cutting: F-X-01, F-X-02a, F-X-04, F-X-05.
`tests/test_remediation.py` holds the post-review regression suite.

## Known limitations (M1)

* No snapshot/sandbox: analyzers run on the real checkout. `dart analyze` may
  create `.dart_tool/`; the git-integrity check reports it.
* `knip` is not installed on this development host; its integration is verified
  against faithful stubs plus a test proving it is never taken from `PATH`.
  `ruff`, `vulture` and `dart analyze` are exercised as real binaries.
* Git integrity uses `git status`, which does not report `.gitignore`d paths.
  Every report states this (`git_integrity.ignored_files_tracked: false`).
* Reference analysis for Python and JS is syntactic (AST/regex) plus known
  dynamic patterns. Unknown dynamic wiring can only produce `REVIEW_REQUIRED`
  or `RETAIN`, never `CANDIDATE` — but it can also hide real dead code.
* JS path aliases (`tsconfig` `paths`) are not resolved by the scanner's own
  reference index; `knip` resolves them, and the reference index only adds
  retention.
* Dart `package:` URIs to third-party packages are not "unresolved": only
  repository-internal URIs count as analysis gaps.

### Recording decision: published surface (`__all__`, `__init__.py`, CLI)

`__init__.py` re-exports and `__all__` names are `PUBLIC` only when the package
really is the published surface: the project declares `[project]`, the
directory is a detected top-level package, and its name matches the declared
distribution (PEP 503-normalised, or a token of it — `mylib14` in
`dch-mylib14`). Everything else is `INTERNAL_SHARED` → `REVIEW_REQUIRED`.

Consequences:

* F-PY-14 (`mylib14` / `dch-mylib14`) → `PROTECTED`.
* F-PY-03b (`app` / `dch-fpy03b`) → `INTERNAL_SHARED` → `REVIEW_REQUIRED`,
  because the package name is unrelated to the distribution name.
* Names with a leading underscore are never `PUBLIC`, wherever they are
  re-exported.
* Shipping a console script (`[project.scripts]`) is an *entrypoint*, not
  evidence that the project is an application: a distributed library that also
  ships a CLI keeps its library surface.

Both resolutions stay conservative: no path in this area can reach `CANDIDATE`.
