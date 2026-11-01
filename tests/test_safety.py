"""Execution safety: allowlist, no shell, environment scrubbing, secrets."""

from __future__ import annotations

import os
import sys

import pytest

from dead_code_hunter.safety import is_secret_path, scrub_env
from dead_code_hunter.tools import Command, run, wrap_executable


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "prod.env",
        "server.pem",
        "server.key",
        "id_rsa",
        "id_rsa.pub",
        "credentials.json",
        "secrets.yaml",
        "app.secret",
        ".npmrc",
    ],
)
def test_secret_files_are_recognized(name):
    assert is_secret_path(name)


@pytest.mark.parametrize("name", ["main.py", "app.ts", "pubspec.yaml", "README.md", "environment.py"])
def test_normal_files_are_not_secret(name):
    assert not is_secret_path(name)


def test_environment_is_scrubbed_but_usable():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "AWS_SECRET_ACCESS_KEY": "x",
        "GITHUB_TOKEN": "x",
        "DATABASE_URL": "postgres://x",
        "IBKR_PASSWORD": "x",
        "MY_API_KEY": "x",
        "SOME_FLAG": "1",
    }
    env = scrub_env(base)
    assert env["PATH"] == "/usr/bin"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "IBKR_PASSWORD" not in env
    assert "MY_API_KEY" not in env
    assert "SOME_FLAG" not in env
    assert env["CI"] == "1"
    assert env["NO_COLOR"] == "1"
    assert env["npm_config_offline"] == "true"


def test_subprocess_does_not_see_credentials(tmp_path):
    script = tmp_path / "dump_env.py"
    script.write_text(
        "import json, os\nprint(json.dumps(dict(os.environ)))\n", encoding="utf-8"
    )
    base = dict(os.environ)
    base["DEPLOY_TOKEN"] = "should-not-leak"
    env = scrub_env(base)
    result = run(
        Command(sys.executable, (str(script),), tool="ruff"), [], tmp_path, env=env
    )
    assert result.ok
    assert "should-not-leak" not in result.stdout
    assert "DEPLOY_TOKEN" not in result.stdout


def test_non_allowlisted_executable_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        run(Command("curl", ("https://example.invalid",)), [], tmp_path)


def test_argv_is_never_interpreted_by_a_shell(tmp_path):
    script = tmp_path / "echo_args.py"
    script.write_text("import sys\nprint(' '.join(sys.argv[1:]))\n", encoding="utf-8")
    result = run(
        Command(sys.executable, (str(script),), tool="vulture"),
        ["a", "&", "echo", "PWNED"],
        tmp_path,
    )
    assert result.ok
    assert "PWNED" in result.stdout  # passed as an argument…
    assert result.stdout.count("PWNED") == 1  # …not executed as a second command


def test_timeout_is_reported_not_raised(tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    result = run(Command(sys.executable, (str(script),), tool="ruff"), [], tmp_path, timeout=1)
    assert result.status == "timeout"
    assert result.exit_code is None


def test_missing_executable_is_an_error_not_a_crash(tmp_path):
    result = run(Command(str(tmp_path / "does-not-exist"), tool="dart"), [], tmp_path)
    assert result.status == "error"
    assert result.error


def test_wrap_executable_uses_cmdexe_for_batch_shims(monkeypatch):
    if os.name != "nt":
        pytest.skip("only relevant on Windows")
    command = wrap_executable(r"C:\repo\node_modules\.bin\knip.cmd")
    assert command.executable.lower().endswith("cmd.exe")
    assert command.prefix_args[0] == "/c"


def test_tool_versions_are_recorded(tmp_path):
    from dead_code_hunter.tools import tool_version

    script = tmp_path / "version.py"
    script.write_text("print('fake-analyzer 1.2.3')\n", encoding="utf-8")
    version = tool_version(Command(sys.executable, (str(script),), tool="ruff"), ["--version"], tmp_path)
    assert version == "1.2.3"


def test_project_virtualenv_is_preferred_over_path(tmp_path):
    """PY-3: the project's own .venv wins, then PATH."""
    from dead_code_hunter.tools import find_python_tool

    repo = tmp_path / "repo"
    local = repo / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    local.mkdir(parents=True)
    candidate = local / ("ruff.exe" if os.name == "nt" else "ruff")
    candidate.write_text("", encoding="utf-8")
    found = find_python_tool("ruff", repo, repo)
    assert found is not None
    assert found.executable.endswith(candidate.name)

    nested = repo / "packages" / "api"
    nested.mkdir(parents=True)
    from_nested = find_python_tool("ruff", nested, repo)
    assert from_nested is not None and from_nested.executable.endswith(candidate.name)


def test_tool_lookup_never_returns_knip_from_path(tmp_path):
    """JS-1: only node_modules/.bin/knip counts."""
    from dead_code_hunter.tools import find_local_knip

    repo = tmp_path / "repo"
    repo.mkdir()
    assert find_local_knip(repo, repo) is None
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / ("knip.cmd" if os.name == "nt" else "knip")).write_text("", encoding="utf-8")
    assert find_local_knip(repo, repo) is not None
