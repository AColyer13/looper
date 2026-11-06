import json
import os
import stat

import pytest
from typer.testing import CliRunner

from looper import credentials
from looper.cli import app
from looper.http_runner import HttpRunner
from looper.policy import AgentSpec
from looper.runners import CallResult, DispatchRunner, Invocation

runner = CliRunner()
KEY = "sk-secret-abcdefghijklmnop-WXYZ"


def agent(style, **kw):
    base = {"anthropic": "https://api.anthropic.com/v1", "ollama": "http://localhost:11434"}.get(style, "https://x/v1")
    d = dict(name="a", kind="api", provider="p", model="m-1", base_url=base, api_key_env="TEST_KEY", api_style=style)
    d.update(kw)
    return AgentSpec(**d)


def recorder(status, payload):
    seen = {}

    def t(method, url, headers, body, timeout):
        seen.update(method=method, url=url, headers=headers, body=body, timeout=timeout)
        return status, payload
    t.seen = seen
    return t


@pytest.mark.parametrize("style,url,payload", [
    ("openai", "https://x/v1/chat/completions", {"choices": [{"message": {"content": "hi"}}]}),
    ("openai_responses", "https://x/v1/responses",
     {"output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]}),
    ("anthropic", "https://api.anthropic.com/v1/messages", {"content": [{"type": "text", "text": "hi"}]}),
    ("ollama", "http://localhost:11434/api/chat", {"message": {"role": "assistant", "content": "hi"}}),
])
def test_http_styles(style, url, payload):
    t = recorder(200, payload)
    r = HttpRunner(transport=t, key_lookup=lambda n: KEY)
    res = r(Invocation(agent(style), "", "hello", 30, max_tokens=99, temperature=0.5, num_ctx=8192,
                       disable_thinking=True))
    assert res.ok and res.output == "hi", res.error
    assert t.seen["url"] == url and t.seen["method"] == "POST"
    body = t.seen["body"]
    assert body["model"] == "m-1"
    if style == "anthropic":
        assert t.seen["headers"]["x-api-key"] == KEY and "anthropic-version" in t.seen["headers"]
        assert body["max_tokens"] == 99 and body["messages"][0]["content"] == "hello"
    else:
        assert t.seen["headers"]["Authorization"] == f"Bearer {KEY}"
    if style == "ollama":
        assert body["options"] == {"num_ctx": 8192, "num_predict": 99, "temperature": 0.5}
        assert body["think"] is False and body["stream"] is False
    if style == "openai_responses":
        assert body["input"] == "hello" and body["max_output_tokens"] == 99
    assert KEY not in " ".join(res.argv)


def test_http_missing_key_and_errors_are_scrubbed():
    r = HttpRunner(transport=recorder(200, {}), key_lookup=lambda n: None)
    res = r(Invocation(agent("openai", provider="openai"), "", "x", 5))
    assert not res.ok and "missing API key $TEST_KEY" in res.error
    r = HttpRunner(transport=recorder(401, {"error": {"message": f"bad key {KEY}"}}), key_lookup=lambda n: KEY)
    res = r(Invocation(agent("openai"), "", "x", 5))
    assert not res.ok and res.returncode == 401 and KEY not in res.error and "HTTP 401" in res.error
    r = HttpRunner(transport=recorder(200, {"choices": [{"message": {"content": ""}}]}), key_lookup=lambda n: KEY)
    assert "empty response" in r(Invocation(agent("openai"), "", "x", 5)).error


def test_http_local_without_key_and_network_error():
    def boom(*a):
        raise OSError("connection refused")
    r = HttpRunner(transport=boom, key_lookup=lambda n: None)
    res = r(Invocation(agent("ollama", kind="local", api_key_env=None), "", "x", 5))
    assert not res.ok and "connection refused" in res.error


def test_dispatch_runner_routes_by_kind():
    cli_calls, http_calls = [], []
    d = DispatchRunner(cli=lambda inv: cli_calls.append(inv) or CallResult("c", [], True, "c"),
                       http=lambda inv: http_calls.append(inv) or CallResult("h", [], True, "h"))
    d(Invocation(AgentSpec(name="claude", binary="claude", command=["{binary}", "{prompt}"]), "/c", "x", 1))
    d(Invocation(agent("openai"), "", "x", 1))
    assert len(cli_calls) == 1 and len(http_calls) == 1


# --------------------------------------------------------------------------- credentials

def test_credentials_file_perms_and_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPER_HOME", str(tmp_path / "lh"))
    where = credentials.set_key("ANTHROPIC_API_KEY", KEY)
    p = tmp_path / "lh" / "credentials.env"
    assert where == str(p)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert credentials.get_key("ANTHROPIC_API_KEY") == KEY and credentials.key_source("ANTHROPIC_API_KEY") == "file"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env-1234567890")
    assert credentials.get_key("ANTHROPIC_API_KEY") == "from-env-1234567890"
    assert credentials.key_source("ANTHROPIC_API_KEY") == "env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert credentials.mask(KEY) == "••••WXYZ" and credentials.mask("short") == "••••"
    assert credentials.remove_key("ANTHROPIC_API_KEY") == ["file"]
    assert credentials.get_key("ANTHROPIC_API_KEY") is None
    with pytest.raises(ValueError):
        credentials.set_key("bad name", "x")
    with pytest.raises(ValueError):
        credentials.set_key("OK_NAME", "a\nb")


def test_keys_cli_never_prints_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPER_HOME", str(tmp_path / "lh"))
    r = runner.invoke(app, ["keys", "set", "xai", "--stdin"], input=KEY + "\n")
    assert r.exit_code == 0, r.output
    assert KEY not in r.output and "••••WXYZ" in r.output
    r = runner.invoke(app, ["keys", "set", "anthropic"], input=KEY + "\n")  # hidden prompt
    assert r.exit_code == 0 and KEY not in r.output
    r = runner.invoke(app, ["keys", "list", "--json"])
    rows = {x["name"]: x for x in json.loads(r.output)}
    assert rows["XAI_API_KEY"]["source"] == "file" and rows["XAI_API_KEY"]["masked"] == "••••WXYZ"
    assert rows["OPENAI_API_KEY"]["source"] == "missing"
    assert KEY not in r.output
    r = runner.invoke(app, ["keys", "list"])
    assert KEY not in r.output and "XAI_API_KEY" in r.output
    r = runner.invoke(app, ["keys", "remove", "xai"])
    assert "removed $XAI_API_KEY" in r.output
    assert runner.invoke(app, ["keys", "set", "ollama", "--stdin"], input="x\n").exit_code == 2
