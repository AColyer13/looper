"""Tool-calling: native mapping, json_block parsing, tier policy, multi-step, CLI, HTTP."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeRunner, team_available, team_policy
from looper import api
from looper import tools as toolmod
from looper.cli import app
from looper.http_runner import HttpRunner, build_request
from looper.policy import AgentSpec
from looper.runners import CallResult, Invocation, SubprocessRunner
from looper.serve import serve

runner = CliRunner()

WRITE_FILE = {
    "name": "write_file",
    "description": "Write a file",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
}
RUN_CMD = {
    "name": "run_command",
    "description": "Run a command",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}
TOOLS = [WRITE_FILE, RUN_CMD]


def _specs():
    return toolmod.parse_tools(TOOLS)


# --------------------------------------------------------------------------- parsing / format


def test_format_tool_call_flattened_and_args():
    tc = toolmod.format_tool_call("write_file", {"path": "a.js", "content": "x"}, call_id="call_1")
    assert tc["id"] == "call_1"
    assert tc["tool"] == "write_file"
    assert tc["path"] == "a.js" and tc["content"] == "x"
    assert tc["args"] == {"path": "a.js", "content": "x"}


def test_parse_json_block_valid_and_strips_text():
    text = (
        "Here's the change...\n\n"
        "```looper-tools\n"
        '{"tool_calls":[{"tool":"write_file","path":"src/auth.js","content":"ok"},'
        '{"tool":"run_command","command":"npm test"}]}\n'
        "```\n"
    )
    clean, calls, warnings, finish = toolmod.parse_json_block(text, _specs())
    assert "Here's the change" in clean
    assert "looper-tools" not in clean
    assert len(calls) == 2
    assert calls[0]["tool"] == "write_file" and calls[0]["path"] == "src/auth.js"
    assert calls[0]["args"]["path"] == "src/auth.js"
    assert calls[1]["command"] == "npm test"
    assert warnings == []


def test_parse_json_block_malformed_unknown_trailing_comma():
    specs = _specs()
    clean, calls, w, finish = toolmod.parse_json_block(
        'say hi\n```looper-tools\n{not json}\n```', specs
    )
    assert calls == [] and any("malformed" in x for x in w)

    clean, calls, w, finish = toolmod.parse_json_block(
        '```looper-tools\n{"tool_calls":[{"tool":"nope","x":1}]}\n```', specs
    )
    assert calls == [] and any("unknown tool" in x for x in w)

    # trailing comma tolerated
    clean, calls, w, finish = toolmod.parse_json_block(
        '```looper-tools\n{"tool_calls":[{"tool":"run_command","command":"ls",},]}\n```',
        specs,
    )
    assert len(calls) == 1 and calls[0]["command"] == "ls"


def test_validate_missing_required_args():
    calls, w = toolmod.validate_tool_calls(
        [{"tool": "write_file", "path": "a.js"}], _specs()
    )
    assert calls == [] and any("missing required" in x for x in w)


def test_plain_text_fallback_no_block():
    clean, calls, w, finish = toolmod.parse_json_block("just a reply", _specs())
    assert clean == "just a reply" and calls == [] and w == []


# --------------------------------------------------------------------------- protocol / tier


def test_choose_protocol_tier_policy():
    specs = _specs()
    proto, w = toolmod.choose_protocol(
        tools=specs, agent_kind="local", api_style="ollama", tier_name="tiny",
        structured_output="unreliable",
    )
    assert proto == "none" and w

    proto, w = toolmod.choose_protocol(
        tools=specs, agent_kind="local", api_style="ollama", tier_name="small",
        structured_output="fair",
    )
    assert proto == "json_block"

    proto, w = toolmod.choose_protocol(
        tools=specs, agent_kind="api", api_style="anthropic", tier_name="frontier",
        structured_output="reliable",
    )
    assert proto == "native"

    proto, w = toolmod.choose_protocol(
        tools=specs, agent_kind="cli", api_style=None, tier_name="frontier",
    )
    assert proto == "json_block"


def test_tiny_tier_refusal_in_ask():
    pol = team_policy(["ollama:tiny-brain:0.5b#tiny"])
    name = pol.team[0]
    fake = FakeRunner(lambda a, p, n: "summary only")
    res = api.ask(
        pol, name, "write a file",
        tools=TOOLS,
        runner=fake,
        available={name: "http://localhost:11434"},
    )
    assert res["ok"]
    assert res["protocol"] == "none"
    assert res["tool_calls"] == []
    assert res["finish_reason"] == "stop"
    assert any("tiny" in x for x in res["warnings"])
    # tools must not have been offered to the runner
    assert fake.calls[0].protocol == "none"
    assert fake.calls[0].tools == []


# --------------------------------------------------------------------------- native HTTP mapping


def _agent(style, **kw):
    base = {
        "anthropic": "https://api.anthropic.com/v1",
        "ollama": "http://localhost:11434",
        "openai_responses": "https://api.openai.com/v1",
    }.get(style, "https://x/v1")
    d = dict(
        name="a", kind="api", provider="p", model="m-1", base_url=base,
        api_key_env="TEST_KEY", api_style=style,
    )
    d.update(kw)
    return AgentSpec(**d)


def _recorder(status, payload):
    seen = {}

    def t(method, url, headers, body, timeout):
        seen.update(method=method, url=url, headers=headers, body=body, timeout=timeout)
        return status, payload

    t.seen = seen
    return t


def test_native_anthropic_request_and_extract():
    specs = _specs()
    payload = {
        "content": [
            {"type": "text", "text": "I'll write it."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "write_file",
                "input": {"path": "a.js", "content": "x"},
            },
        ],
        "stop_reason": "tool_use",
    }
    t = _recorder(200, payload)
    inv = Invocation(
        _agent("anthropic"), "", "write a.js", 30,
        messages=[{"role": "user", "content": "write a.js"}],
        tools=specs, protocol="native",
    )
    res = HttpRunner(transport=t, key_lookup=lambda n: "sk")(inv)
    assert res.ok and res.finish_reason == "tool_calls"
    assert res.tool_calls[0]["tool"] == "write_file"
    assert res.tool_calls[0]["id"] == "toolu_1"
    assert res.tool_calls[0]["args"]["path"] == "a.js"
    body = t.seen["body"]
    assert body["tools"][0]["name"] == "write_file"
    assert body["tools"][0]["input_schema"]["required"] == ["path", "content"]


def test_native_openai_chat_request_and_extract():
    specs = _specs()
    payload = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_9",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": '{"command":"npm test"}',
                    },
                }],
            },
        }]
    }
    t = _recorder(200, payload)
    inv = Invocation(
        _agent("openai"), "", "test", 30,
        messages=[{"role": "user", "content": "test"}],
        tools=specs, protocol="native",
    )
    res = HttpRunner(transport=t, key_lookup=lambda n: "sk")(inv)
    assert res.ok and res.finish_reason == "tool_calls"
    assert res.tool_calls[0]["tool"] == "run_command"
    assert res.tool_calls[0]["command"] == "npm test"
    assert t.seen["body"]["tools"][0]["type"] == "function"


def test_native_openai_responses_request_and_extract():
    specs = _specs()
    payload = {
        "output": [
            {"type": "function_call", "call_id": "fc_1", "name": "run_command",
             "arguments": '{"command":"ls"}'},
        ],
        "status": "completed",
    }
    t = _recorder(200, payload)
    inv = Invocation(
        _agent("openai_responses"), "", "ls", 30,
        messages=[{"role": "user", "content": "ls"}],
        tools=specs, protocol="native",
    )
    res = HttpRunner(transport=t, key_lookup=lambda n: "sk")(inv)
    assert res.ok and res.tool_calls[0]["id"] == "fc_1"
    assert t.seen["body"]["tools"][0]["type"] == "function"
    assert isinstance(t.seen["body"]["input"], list)


def test_native_ollama_request_and_extract():
    specs = _specs()
    payload = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {"name": "write_file", "arguments": {"path": "a", "content": "b"}},
            }],
        }
    }
    t = _recorder(200, payload)
    inv = Invocation(
        _agent("ollama", kind="local", api_key_env=None, key_optional=True), "", "x", 30,
        messages=[{"role": "user", "content": "x"}],
        tools=specs, protocol="native",
    )
    res = HttpRunner(transport=t, key_lookup=lambda n: None)(inv)
    assert res.ok and res.tool_calls[0]["path"] == "a"
    assert "tools" in t.seen["body"]


def test_http_json_block_protocol_parses_fence():
    specs = _specs()
    text = 'done\n```looper-tools\n{"tool_calls":[{"tool":"run_command","command":"echo hi"}]}\n```'
    t = _recorder(200, {"choices": [{"message": {"content": text}}]})
    inv = Invocation(
        _agent("openai"), "", "x", 30,
        messages=[{"role": "user", "content": "x"}],
        tools=specs, protocol="json_block",
    )
    res = HttpRunner(transport=t, key_lookup=lambda n: "sk")(inv)
    assert res.ok and res.protocol == "json_block"
    assert res.tool_calls[0]["command"] == "echo hi"
    assert "tools" not in t.seen["body"]  # json_block does not send native tools


# --------------------------------------------------------------------------- multi-step + ask


def test_ask_multistep_round_trip_with_tool_results():
    pol = team_policy(["anthropic:claude-sonnet-4-5"])
    name = pol.team[0]
    # Force json_block via protocol so FakeRunner text is enough
    block = (
        'Working.\n```looper-tools\n'
        '{"tool_calls":[{"tool":"write_file","path":"x.js","content":"1"}]}\n```'
    )

    def resp(agent, prompt, n):
        if n == 1:
            return block
        return "All done."

    fake = FakeRunner(resp)
    avail = {name: "https://api.anthropic.com/v1"}
    r1 = api.ask(
        pol, name, "write x.js",
        tools=TOOLS, protocol="json_block",
        runner=fake, available=avail,
    )
    assert r1["ok"] and r1["finish_reason"] == "tool_calls"
    assert r1["tool_calls"][0]["tool"] == "write_file"
    assert r1["messages"][-1]["role"] == "assistant"
    assert r1["messages"][-1]["tool_calls"]

    r2 = api.ask(
        pol, name,
        tools=TOOLS, protocol="json_block",
        messages=r1["messages"],
        tool_results=[{
            "id": r1["tool_calls"][0]["id"],
            "tool": "write_file",
            "ok": True,
            "output": "wrote x.js",
        }],
        runner=fake, available=avail,
    )
    assert r2["ok"] and r2["finish_reason"] == "stop"
    assert r2["tool_calls"] == []
    assert "All done" in r2["text"]
    # second call's conversation includes a tool result
    roles = [m["role"] for m in fake.calls[1].messages]
    assert "tool" in roles or "tool_result" in str(fake.calls[1].prompt)


def test_ask_plain_text_fallback_no_tools():
    pol = team_policy(["cli:codex"])
    name = pol.team[0]
    fake = FakeRunner(lambda a, p, n: "hello")
    res = api.ask(pol, name, "hi", runner=fake, available={name: "/usr/bin/codex"})
    assert res["ok"] and res["text"] == "hello"
    assert res["tool_calls"] == [] and res["finish_reason"] == "stop"
    assert res["protocol"] == "none"


def test_ask_cli_json_block_with_fake_cli(tmp_path):
    """Fake CLI binary prints a looper-tools block; ask parses it."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('I will write it.')\n"
        "print('```looper-tools')\n"
        "print('{\"tool_calls\":[{\"tool\":\"write_file\",\"path\":\"a.py\",\"content\":\"print(1)\"}]}')\n"
        "print('```')\n"
    )
    script.chmod(0o755)
    pol = team_policy(["cli:claude"])
    name = "claude"
    # override binary via available path
    res = api.ask(
        pol, name, "write a.py",
        tools=TOOLS,
        runner=SubprocessRunner(),
        available={name: str(script)},
        timeout=10,
    )
    assert res["ok"], res.get("error")
    assert res["protocol"] == "json_block"
    assert res["finish_reason"] == "tool_calls"
    assert res["tool_calls"][0]["path"] == "a.py"
    assert "I will write" in res["text"]
    # plan-mode flags should appear in argv used (CallResult.argv)
    # SubprocessRunner returns argv; check via a second dry prepare
    from looper.runners import prepare_invocation
    inv = prepare_invocation(
        pol.agents[name], str(script), "x", 5,
        extra_argv=toolmod.cli_extra_args_for_caller_tools("claude"),
    )
    assert "--permission-mode" in inv.argv and "plan" in inv.argv


def test_version_lists_tool_capabilities():
    r = runner.invoke(app, ["version", "--json"])
    data = json.loads(r.output)
    assert data["version"] == "0.7.0"
    assert data["api_version"] == 1
    assert "tool_calls" in data["capabilities"]
    assert "tool_results" in data["capabilities"]
    assert "history_trim" in data["capabilities"]
    assert "ask_tools" in data["capabilities"]


def test_http_ask_with_tools(tmp_path, monkeypatch):
    pol = team_policy(["cli:codex"])
    name = pol.team[0]
    block = (
        'ok\n```looper-tools\n'
        '{"tool_calls":[{"tool":"run_command","command":"npm test"}]}\n```'
    )

    class _Run:
        def __call__(self, inv):
            return CallResult(inv.agent.name, inv.argv, True, block, protocol="json_block")

    monkeypatch.setattr("looper.api.DispatchRunner", lambda: _Run())
    # also patch serve's import path by patching api.ask's default — already uses DispatchRunner()

    httpd = serve("127.0.0.1", 0, config_path=None)
    host, port = httpd.server_address
    import threading
    from http.client import HTTPConnection

    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        # Force team via body; agent must exist in default policy — use codex from override
        body = {
            "agent": "codex",
            "prompt": "run tests",
            "tools": TOOLS,
            "protocol": "json_block",
            "team": {"agents": {"codex": {"kind": "cli", "binary": "codex",
                                          "command": ["{binary}", "{prompt}"]}},
                     "team": ["codex"]},
        }
        # Without a ready endpoint ask will fail; patch agent_endpoint / default_available
        monkeypatch.setattr(
            "looper.api.default_available",
            lambda policy: ({"codex": "/usr/bin/codex"}, {}),
        )
        monkeypatch.setattr(
            "looper.api.agent_endpoint",
            lambda agent: ("/usr/bin/codex", None),
        )
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/ask", body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        assert resp.status == 200, data
        assert data["ok"] and data["tool_calls"][0]["command"] == "npm test"
        assert data["finish_reason"] == "tool_calls"
        conn.close()
    finally:
        httpd.shutdown()


def test_cli_request_json_stdin():
    pol_team = {
        "agents": {"codex": {"kind": "cli", "binary": "codex", "command": ["{binary}", "{prompt}"]}},
        "team": ["codex"],
    }
    req = {
        "prompt": "write hi",
        "tools": TOOLS,
        "protocol": "json_block",
        "team": pol_team,
    }

    class _Run:
        def __call__(self, inv):
            return CallResult(
                "codex", inv.argv, True,
                '```looper-tools\n{"tool_calls":[{"tool":"write_file","path":"hi","content":"1"}]}\n```',
                protocol="json_block",
            )

    import looper.api as api_mod
    # Patch at DispatchRunner construction inside ask
    from unittest.mock import patch

    with patch("looper.api.DispatchRunner", lambda: _Run()), \
         patch("looper.api.default_available", lambda policy: ({"codex": "/bin/codex"}, {})), \
         patch("looper.api.agent_endpoint", lambda agent: ("/bin/codex", None)):
        r = runner.invoke(
            app,
            ["ask", "--json", "--agent", "codex", "--team-json", json.dumps(pol_team),
             "--request-json", "-"],
            input=json.dumps(req),
        )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["tool_calls"][0]["tool"] == "write_file"
    assert data["tool_calls"][0]["args"]["path"] == "hi"


def test_build_request_tool_results_anthropic_roundtrip():
    specs = _specs()
    msgs = toolmod.seed_messages(
        prompt=None,
        system=None,
        messages=[
            {"role": "user", "content": "write"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    toolmod.format_tool_call("write_file", {"path": "a", "content": "b"}, call_id="toolu_1")
                ],
            },
        ],
        tool_results=[{"id": "toolu_1", "tool": "write_file", "ok": True, "output": "ok"}],
    )
    inv = Invocation(
        _agent("anthropic"), "", "", 30,
        messages=msgs, tools=specs, protocol="native",
    )
    url, headers, body = build_request(inv, "sk")
    # last user message should contain tool_result
    assert any(
        isinstance(m.get("content"), list)
        and any(c.get("type") == "tool_result" for c in m["content"])
        for m in body["messages"]
    )


# --------------------------------------------------------------------------- invalid_tool_calls finish_reason


def test_openai_chat_finish_invalid_when_all_calls_rejected():
    """Provider said tool_calls but every call failed validation → invalid_tool_calls."""
    tools = toolmod.parse_tools([{
        "name": "Bash", "description": "x",
        "parameters": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]},
    }])
    raw = {"choices": [{"message": {
        "content": "I'll run it",
        "tool_calls": [{"id": "a", "type": "function",
                        "function": {"name": "Bash", "arguments": "{}"}}]
    }, "finish_reason": "tool_calls"}]}
    text, calls, finish, warnings = toolmod.extract_native_openai_chat(raw, tools, max_calls=None)
    assert text == "I'll run it"
    assert calls == []
    assert finish == "invalid_tool_calls"
    assert any("missing required" in w for w in warnings)


def test_ollama_finish_invalid_when_all_calls_rejected():
    tools = toolmod.parse_tools([{
        "name": "Bash", "description": "x",
        "parameters": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]},
    }])
    raw = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "Bash", "arguments": {}}}
    ]}}
    text, calls, finish, warnings = toolmod.extract_native_ollama(raw, tools, max_calls=None)
    assert calls == [] and finish == "invalid_tool_calls"


def test_anthropic_finish_invalid_when_all_calls_rejected():
    tools = toolmod.parse_tools([{
        "name": "Bash", "description": "x",
        "parameters": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]},
    }])
    raw = {"content": [
        {"type": "text", "text": "x"},
        {"type": "tool_use", "id": "1", "name": "Bash", "input": {}},
    ], "stop_reason": "tool_use"}
    text, calls, finish, warnings = toolmod.extract_native_anthropic(raw, tools, max_calls=None)
    assert calls == [] and finish == "invalid_tool_calls"


def test_json_block_finish_invalid_when_all_calls_rejected():
    tools = _specs()
    text = 'nope\n\n```looper-tools\n{"tool_calls":[{"tool":"write_file","path":"a"}]}\n```'
    clean, calls, w, finish = toolmod.parse_json_block(text, tools)
    assert calls == [] and finish == "invalid_tool_calls"
    assert any("missing required" in x for x in w)
