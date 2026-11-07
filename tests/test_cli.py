import json

import pytest
from typer.testing import CliRunner

from looper import cli
from looper.cli import _default_to_run, app

runner = CliRunner()
CMDS = {"run", "route", "agents", "loops", "show", "init", "validate", "version", "doctor", "plan", "ask", "serve", "setup", "models", "spec", "keys"}


@pytest.fixture(autouse=True)
def fake_path(monkeypatch, tmp_path):
    """Pretend only claude + codex are installed, via fake binaries on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        p = bindir / name
        p.write_text(f"#!/bin/sh\necho '{name} 1.0'\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))


@pytest.mark.parametrize("argv,expected", [
    (["--loop", "tdd", "add a test"], ["run", "--loop", "tdd", "add a test"]),
    (["add a test", "--dry-run"], ["run", "add a test", "--dry-run"]),
    (["--config=x.json", "do it"], ["run", "--config=x.json", "do it"]),
    (["route", "fix bug"], ["route", "fix bug"]),
    (["agents"], ["agents"]),
    (["--version"], ["--version"]),
    (["--help"], ["--help"]),
    (["--dry-run"], ["run", "--dry-run"]),
])
def test_default_to_run(argv, expected):
    assert _default_to_run(argv, CMDS) == expected


def test_root_loop_form_dry_run():
    r = runner.invoke(app, ["--loop", "tdd", "add a test for the login flow", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "DRY RUN" in r.output and "tdd" in r.output and "write_test" in r.output


def test_root_loop_form_json():
    r = runner.invoke(app, ["--loop", "tdd", "--dry-run", "--json", "add a test for the login flow"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["loop"] == "tdd" and plan["route"]["method"] == "override"
    assert plan["stages"][0]["agent"] == "codex"


def test_run_dry_run_routes():
    r = runner.invoke(app, ["run", "implement a rate limiter", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["loop"] == "explore_plan_code_commit"
    assert plan["stages_without_agent"] == []


def test_route_command():
    r = runner.invoke(app, ["route", "fix the bug in auth.py"])
    assert r.exit_code == 0 and "hypothesis_tool_scout" in r.output
    r = runner.invoke(app, ["route", "--json", "production is down 500 errors"])
    assert json.loads(r.output)["loop"] == "incident_response"


def test_agents_command():
    r = runner.invoke(app, ["agents", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["claude"]["installed"] and data["claude"]["version"] == "claude 1.0"
    assert not data["grok"]["installed"]
    r = runner.invoke(app, ["agents"])
    assert "missing" in r.output and "installed" in r.output


def test_loops_and_show():
    r = runner.invoke(app, ["loops", "--json"])
    assert len(json.loads(r.output)) == 14
    r = runner.invoke(app, ["show", "musk_algorithm"])
    assert r.exit_code == 0 and "question_requirements" in r.output
    r = runner.invoke(app, ["show", "nope"])
    assert r.exit_code == 2


def test_init_and_validate(tmp_path):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and (tmp_path / "looper.json").exists()
    assert runner.invoke(app, ["init"]).exit_code == 1
    r = runner.invoke(app, ["validate"])
    assert r.exit_code == 0 and "14 loops" in r.output and "./looper.json" in r.output


def test_real_run_with_injected_runner(monkeypatch, tmp_path):
    from conftest import FakeRunner
    fake = FakeRunner(lambda a, p, n: "ALL TESTS PASS")
    monkeypatch.setattr("looper.executor.SubprocessRunner", lambda **kw: fake)
    r = runner.invoke(app, ["--loop", "tdd", "--agent", "claude", "--max-iterations", "2", "--json", "add a test"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["status"] == "ok" and res["iterations"] == 1
    assert set(fake.agents) == {"claude"}
    assert list((tmp_path / ".looper" / "runs").glob("*-tdd.json"))


def test_unknown_agent_flag():
    r = runner.invoke(app, ["--agent", "gpt", "--dry-run", "x"])
    assert r.exit_code == 2


def test_version():
    r = runner.invoke(app, ["--version"])
    assert "0.7.1" in r.output
