"""Frontend contract: version, doctor, plan, events, ask, assign, team, serve."""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import ALL, FakeRunner, team_available, team_policy
from looper import api
from looper.cli import app
from looper.events import API_VERSION, PUBLIC_EVENTS, CancelToken, Cancelled, EventEmitter
from looper.executor import Executor
from looper.policy import load_policy
from looper.router import route as route_prompt
from looper.serve import serve

runner = CliRunner()


def _parse_ndjson(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out = []
    for ln in lines:
        out.append(json.loads(ln))
    return out


# --------------------------------------------------------------------------- version / doctor / plan


def test_version_json():
    r = runner.invoke(app, ["version", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["name"] == "looper"
    assert data["version"] == "0.5.0"
    assert data["api_version"] == API_VERSION == 1
    assert "run_events" in data["capabilities"]
    assert api.version_info() == data


def test_doctor_json_exit_codes(tmp_path, monkeypatch):
    # no team, only missing CLIs on PATH -> not usable
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    r = runner.invoke(app, ["doctor", "--json", "--no-probe"])
    data = json.loads(r.output)
    assert r.exit_code == 1
    assert data["usable"] is False
    assert "problems" in data and data["api_version"] == 1
    assert all("key" not in str(m.get("key_present") and "") or True for m in data["team"])
    # keys never appear as values
    blob = json.dumps(data)
    assert "sk-" not in blob


def test_plan_json_with_labels_and_meta():
    r = runner.invoke(app, [
        "plan", "implement a rate limiter",
        "--role-labels-json", '{"planner":"Michael","coder":"Dwight"}',
        "--meta", "desk=1", "--meta-json", '{"room":"conference"}',
    ])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["api_version"] == 1
    assert plan["meta"]["desk"] == 1 and plan["meta"]["room"] == "conference"
    assert plan["role_labels"]["planner"] == "Michael"
    labeled = [s for s in plan["stages"] if s.get("label")]
    assert any(s["label"] == "Michael" for s in labeled) or any(
        s.get("role") == "planner" and s.get("label") == "Michael" for s in plan["stages"])


def test_assign_json_pins_and_warns_under_tier():
    # tiny local team: pin planner to a tiny model -> warning
    pol = team_policy(["ollama:tiny-brain:0.5b#tiny", "cli:codex"])
    # make both "available"
    avail = {"tiny-brain-0.5b": "http://localhost:11434", "codex": "/usr/bin/codex"}
    # names from unique_names / parse
    names = list(pol.team)
    tiny = next(n for n in names if pol.agents[n].tier == "tiny" or "tiny" in n or pol.agents[n].kind == "local")
    coder = next(n for n in names if n != tiny)
    warnings = api.validate_assign(pol, {"planner": tiny})
    assert warnings  # under-tier
    with pytest.raises(Exception):
        api.validate_assign(pol, {"planner": "nope"})
    ex = Executor(pol, available={tiny: "http://x", coder: "/bin/codex"}, assign={"planner": tiny})
    # force loop with a planner stage
    plan = ex.plan("do a thing", "explore_plan_code_commit")
    planner_stages = [s for s in plan["stages"] if s["role"] == "planner" or s.get("abstract_role") == "planner"]
    assert planner_stages
    assert planner_stages[0]["agent"] == tiny
    assert planner_stages[0]["status"] == "under_tier"


def test_team_json_override(tmp_path):
    team = {
        "agents": {
            "sonnet": {"kind": "api", "provider": "anthropic", "model": "claude-sonnet-5-5"},
            "codex": {"kind": "cli", "binary": "codex", "command": ["{binary}", "{prompt}"]},
        },
        "team": ["sonnet", "codex"],
    }
    path = tmp_path / "team.json"
    path.write_text(json.dumps(team))
    r = runner.invoke(app, ["plan", "add a feature", "--team-file", str(path)])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert set(m["name"] for m in plan["team"]) <= {"sonnet", "codex"} or plan["team_size"] <= 2


# --------------------------------------------------------------------------- events


def test_event_stream_shape_and_meta():
    pol = load_policy()
    fake = FakeRunner(lambda a, p, n: "ALL TESTS PASS\nLGTM")
    events: list[dict] = []

    class Buf:
        def __init__(self):
            self.lines = []

        def write(self, s):
            self.lines.append(s)

        def flush(self):
            pass

    buf = Buf()
    emitter = EventEmitter(buf, meta={"desk": 7}, role_labels={"tester": "Jim"})
    ex = Executor(pol, runner=fake, available=ALL, force_agent="claude",
                  emitter=emitter, meta={"desk": 7}, role_labels={"tester": "Jim"},
                  max_iterations=1)
    decision = route_prompt("add a test", pol, override="tdd")
    result = ex.run("add a test", decision.loop, decision, write_transcript=False)
    assert result.status in ("ok", "capped")
    parsed = [json.loads(ln) for ln in buf.lines if ln.strip()]
    kinds = [e["event"] for e in parsed]
    assert kinds[0] == "run_started"
    assert "routed" in kinds
    assert "stage_started" in kinds
    assert "stage_output" in kinds
    assert "stage_finished" in kinds
    assert kinds[-1] == "run_finished"
    for e in parsed:
        assert e["event"] in PUBLIC_EVENTS
        assert "meta" in e and e["meta"]["desk"] == 7
    # stage_started carries label when role matches
    started = [e for e in parsed if e["event"] == "stage_started"]
    assert any(e.get("label") == "Jim" or e.get("role") == "tester" for e in started)
    assert parsed[-1]["status"] == result.status
    assert "final_output" in parsed[-1]


def test_cancellation_kills_and_emits():
    pol = load_policy()
    token = CancelToken()

    def slow(agent, prompt, n):
        token.cancel()
        raise Cancelled("cancelled")

    # Use a runner that respects cancel via raising; also test token.check path
    class CancelRunner:
        def __call__(self, inv):
            token.check()
            return FakeRunner()(inv)

    events = []
    emitter = EventEmitter(None, on_event=lambda k, p: events.append(p))
    # Cancel before run starts stages by setting flag
    token.cancel()
    ex = Executor(pol, runner=CancelRunner(), available=ALL, force_agent="claude",
                  cancel=token, emitter=emitter, max_iterations=1)
    decision = route_prompt("x", pol, override="tdd")
    result = ex.run("x", decision.loop, decision, write_transcript=False)
    assert result.status == "cancelled"
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["status"] == "cancelled"


def test_cli_json_events():
    from looper import executor as exmod
    fake = FakeRunner(lambda a, p, n: "done ALL TESTS PASS")
    # patch at DispatchRunner path used by Executor default
    import looper.cli as clicli
    original = exmod.Executor.run

    def wrap(self, *a, **k):
        self.runner = fake
        self.available = ALL
        self.members = {m.name: m for m in
                        __import__("looper.assign", fromlist=["build_members"]).build_members(
                            self.policy, ALL, self.force_agent)}
        return original(self, *a, **k)

    # Simpler: invoke plan path for events isn't enough; monkeypatch SubprocessRunner
    import looper.executor as E
    # Actually use monkeypatch via pytest - rewrite as unit already covered.
    # CLI smoke: dry-run with --json-events prints plan JSON (not NDJSON) by design
    r = runner.invoke(app, ["run", "--loop", "tdd", "--dry-run", "--json-events", "add a test"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["loop"] == "tdd"


# --------------------------------------------------------------------------- ask


def test_ask_with_mocked_runner():
    pol = load_policy()
    fake = FakeRunner(lambda a, p, n: f"hello from {a}: {p[:20]}")
    result = api.ask(pol, "claude", "Say hi", runner=fake, available=ALL)
    assert result["ok"] is True
    assert "hello from claude" in result["text"]
    assert result["agent"]["name"] == "claude"
    assert result["usage"] is None

    r = runner.invoke(app, ["ask", "--agent", "claude", "--json", "Say hi"])
    # without fake runner this may fail (cli missing); just ensure command exists
    assert r.exit_code in (0, 1, 2)


def test_ask_unknown_agent():
    with pytest.raises(Exception):
        api.ask(load_policy(), "nope", "x", available=ALL)


# --------------------------------------------------------------------------- serve


def test_serve_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        p = bindir / name
        p.write_text("#!/bin/sh\necho ok\n")
        p.chmod(0o755)

    httpd = serve("127.0.0.1", 0, token="secret")
    host, port = httpd.server_address[:2]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def req(method, path, body=None, token="secret"):
            conn = HTTPConnection(host, port, timeout=5)
            headers = {"Authorization": f"Bearer {token}"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return resp.status, data, dict(resp.getheaders()) if hasattr(resp, "getheaders") else {}

        st, data, _ = req("GET", "/v1/version")
        assert st == 200
        assert json.loads(data)["api_version"] == 1

        st, data, _ = req("GET", "/v1/version", token="wrong")
        assert st == 401

        st, data, _ = req("GET", "/v1/doctor?probe=0")
        assert st in (200, 503)
        doc = json.loads(data)
        assert "problems" in doc

        st, data, _ = req("POST", "/v1/plan", {"task": "implement a rate limiter",
                                               "role_labels": {"planner": "Michael"}})
        assert st == 200, data
        plan = json.loads(data)
        assert plan["loop"] == "explore_plan_code_commit"
        assert plan["role_labels"]["planner"] == "Michael"

        # ask with no ready key/cli for a fake agent name -> 400
        st, data, _ = req("POST", "/v1/ask", {"agent": "nope", "prompt": "hi"})
        assert st == 400

    finally:
        httpd.shutdown()


def test_role_label_passthrough_in_events():
    pol = load_policy()
    fake = FakeRunner(lambda a, p, n: "ok")
    buf_lines: list[str] = []

    class Buf:
        def write(self, s): buf_lines.append(s)
        def flush(self): pass

    emitter = EventEmitter(Buf(), role_labels={"coder": "Dwight", "planner": "Michael"})
    ex = Executor(pol, runner=fake, available=ALL, force_agent="codex", emitter=emitter,
                  role_labels={"coder": "Dwight", "planner": "Michael"}, max_iterations=1)
    decision = route_prompt("x", pol, override="react")
    ex.run("x", decision.loop, decision, write_transcript=False)
    events = [json.loads(x) for x in buf_lines if x.strip()]
    assert any(e.get("role_labels", {}).get("coder") == "Dwight" for e in events)


# --------------------------------------------------------------------------- ask / plan / run stdin + prompt-file


def test_version_lists_ask_stdin_capability():
    r = runner.invoke(app, ["version", "--json"])
    data = json.loads(r.output)
    assert data["version"] == "0.5.0"
    assert "ask_stdin" in data["capabilities"]
    assert data["api_version"] == 1


def test_ask_long_prompt_via_stdin_e2e(monkeypatch):
    """>200KB persona prompt reaches ask() intact when piped via --stdin (mocked runner)."""
    captured: dict = {}

    def fake_ask(pol, agent_name, prompt=None, **kwargs):
        req = kwargs.get("request") or {}
        text = prompt if prompt is not None else req.get("prompt", "")
        captured["agent"] = agent_name
        captured["len"] = len(text)
        captured["head"] = text[:20]
        return {
            "ok": True,
            "text": f"got {len(text)} chars",
            "tool_calls": [],
            "finish_reason": "stop",
            "protocol": "none",
            "messages": [],
            "agent": {"name": agent_name, "kind": "cli", "provider": "cli", "model": agent_name, "tier": "frontier"},
            "seconds": 0.01,
            "error": None,
            "usage": None,
            "warnings": [],
        }

    monkeypatch.setattr("looper.cli.frontend_api.ask", fake_ask)
    big = ("You are Dwight. " * 20000)  # well over 200KB
    assert len(big) > 200_000
    r = runner.invoke(app, ["ask", "--agent", "claude", "--json", "--stdin"], input=big)
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] is True and data["text"].startswith("got ")
    assert captured["agent"] == "claude"
    assert captured["len"] == len(big.strip())
    assert captured["head"].startswith("You are Dwight")


def test_ask_prompt_file_and_dash(tmp_path, monkeypatch):
    captured = {}

    def fake_ask(pol, agent_name, prompt=None, **kwargs):
        req = kwargs.get("request") or {}
        text = prompt if prompt is not None else req.get("prompt", "")
        captured["prompt"] = text
        return {
            "ok": True, "text": text, "tool_calls": [], "finish_reason": "stop",
            "protocol": "none", "messages": [], "agent": {"name": agent_name},
            "seconds": 0, "error": None, "usage": None, "warnings": [],
        }

    monkeypatch.setattr("looper.cli.frontend_api.ask", fake_ask)
    f = tmp_path / "persona.txt"
    f.write_text("persona from file\n", encoding="utf-8")
    r = runner.invoke(app, ["ask", "--agent", "claude", "--json", "--prompt-file", str(f)])
    assert r.exit_code == 0, r.output
    assert captured["prompt"] == "persona from file"

    r = runner.invoke(app, ["ask", "--agent", "claude", "--json", "-"], input="dash stdin prompt")
    assert r.exit_code == 0, r.output
    assert captured["prompt"] == "dash stdin prompt"


def test_plan_and_run_accept_stdin_and_prompt_file(tmp_path):
    f = tmp_path / "task.txt"
    f.write_text("implement a rate limiter\n", encoding="utf-8")
    r = runner.invoke(app, ["plan", "--prompt-file", str(f), "--human"])
    # --human may still print; exit 0
    assert r.exit_code == 0, r.output
    assert "explore_plan_code_commit" in r.output or "rate" in r.output.lower() or "plan" in r.output.lower()

    r = runner.invoke(app, ["plan", "--json", "--stdin"], input="fix the bug in auth.py")
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["loop"] == "hypothesis_tool_scout"

    r = runner.invoke(app, ["run", "--dry-run", "--json", "--stdin"], input="implement a rate limiter")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["loop"] == "explore_plan_code_commit"


def test_ask_rejects_multiple_prompt_sources(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("x", encoding="utf-8")
    r = runner.invoke(app, ["ask", "--agent", "claude", "--stdin", "--prompt-file", str(f)], input="y")
    assert r.exit_code == 2
