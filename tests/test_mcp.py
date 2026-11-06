"""MCP policy precedence, approval round-trip, and the in-repo stdio server."""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from typer.testing import CliRunner

from conftest import team_policy
from looper import api
from looper.cli import app
from looper.mcp_client import McpHub, parse_sse_body
from looper.mcp_config import (
    McpSettings,
    RolePolicy,
    ServerSpec,
    ToolLists,
    decide,
    load_mcp_settings,
)
from looper.runners import CallResult
from looper.workspace import load_workspace

runner = CliRunner()
SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio_server.py"
SECRET = "super-secret-token-value"


def _settings(**kwargs) -> McpSettings:
    lists = ToolLists(
        deny=list(kwargs.pop("deny", [])),
        require_approval=list(kwargs.pop("require_approval", [])),
        allow=list(kwargs.pop("allow", [])),
        auto_approve=list(kwargs.pop("auto_approve", [])),
    )
    return McpSettings(mode=kwargs.pop("mode", "auto_read_only"), lists=lists)


def _server(name: str = "notes", **kwargs) -> ServerSpec:
    lists = ToolLists(
        deny=list(kwargs.pop("deny", [])),
        require_approval=list(kwargs.pop("require_approval", [])),
        allow=list(kwargs.pop("allow", [])),
        auto_approve=list(kwargs.pop("auto_approve", [])),
    )
    return ServerSpec(name=name, transport="stdio", source="test", command="x", lists=lists, **kwargs)


def _role(**kwargs) -> RolePolicy:
    return RolePolicy(
        mode=kwargs.get("mode"),
        lists=ToolLists(
            deny=list(kwargs.get("deny", [])),
            require_approval=list(kwargs.get("require_approval", [])),
            allow=list(kwargs.get("allow", [])),
            auto_approve=list(kwargs.get("auto_approve", [])),
        ),
    )


def test_policy_precedence_matrix():
    read_only = {"readOnlyHint": True, "destructiveHint": False}
    write = {"readOnlyHint": False}
    destructive = {"readOnlyHint": True, "destructiveHint": True}
    bare = {}
    settings = _settings()
    server = _server()

    assert decide(settings, server, "read_note", read_only) == ("auto", "read_only")
    assert decide(settings, server, "write_note", write) == ("approval", "not_read_only")
    assert decide(settings, server, "wipe", destructive) == ("approval", "destructive")
    assert decide(settings, server, "mystery", bare) == ("approval", "not_read_only")

    # require_approval beats a read-only annotation and beats allow/auto_approve.
    forced = _server(require_approval=["read_note"], auto_approve=["read_note"])
    assert decide(settings, forced, "read_note", read_only)[0] == "approval"

    # allow / auto_approve beat the annotation default, including approve_all and deny_all.
    trusted = _server(auto_approve=["write_note"])
    assert decide(_settings(mode="approve_all"), trusted, "write_note", write) == ("auto", "auto_approve")
    assert decide(_settings(mode="deny_all"), trusted, "write_note", write) == ("auto", "auto_approve")
    allowed = _server(allow=["write_note"])
    assert decide(settings, allowed, "write_note", write) == ("auto", "auto_approve")

    # deny beats require_approval, allow, and auto_approve.
    denied = _server(deny=["write_note"], auto_approve=["write_note"], require_approval=["write_note"])
    assert decide(settings, denied, "write_note", write) == ("deny", "deny")
    assert decide(_settings(deny=["notes:read_note"]), server, "read_note", read_only) == ("deny", "deny")

    # Modes replace the annotation default only.
    assert decide(_settings(mode="approve_all"), server, "read_note", read_only) == ("approval", "approve_all")
    assert decide(_settings(mode="deny_all"), server, "read_note", read_only) == ("deny", "deny_all")

    # A role override is the same precedence, and its deny wins over a server auto_approve.
    role = _role(deny=["write_note"])
    assert decide(settings, trusted, "write_note", write, role) == ("deny", "deny")
    role_mode = _role(mode="approve_all")
    assert decide(settings, server, "read_note", read_only, role_mode) == ("approval", "approve_all")
    # Glob and qualified names.
    assert decide(_settings(require_approval=["notes:read_*"]), server, "read_note", read_only)[0] == "approval"


def test_project_mcp_overrides_claude_and_machine(tmp_path, monkeypatch):
    state = Path.home() / ".looper"
    state.mkdir(parents=True)
    (state / "looper.json").write_text(json.dumps({
        "mcp": {"mode": "deny_all", "servers": {"notes": {"command": "from-machine", "args": ["a"]}}}
    }))
    root = tmp_path / "repo"
    (root / ".looper").mkdir(parents=True)
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"notes": {"command": "from-claude", "args": ["b"]}, "other": {"command": "claude-only"}}
    }))
    (root / ".looper" / "mcp.json").write_text(json.dumps({
        "mode": "auto_read_only",
        "servers": {"notes": {"command": "from-project", "args": ["c"], "include": ["read_*"]}},
    }))
    ws = load_workspace(start=root, project=root, policy_raw={}, policy_path=None)
    assert ws.mcp.mode == "auto_read_only"
    assert ws.mcp.servers["notes"].command == "from-project"
    assert ws.mcp.servers["notes"].include == ["read_*"]
    assert ws.mcp.servers["other"].command == "claude-only"
    # Direct loader agrees.
    loaded = load_mcp_settings({}, None, root)
    assert loaded.servers["notes"].command == "from-project"


def test_env_placeholder_is_not_written_back_and_not_on_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_TOKEN", SECRET)
    root = tmp_path / "repo"
    (root / ".looper").mkdir(parents=True)
    raw = {
        "mcpServers": {
            "notes": {
                "command": sys.executable,
                "args": ["-u", str(SERVER)],
                "env": {"NOTES_TOKEN": "${NOTES_TOKEN}"},
            }
        }
    }
    mcp_path = root / ".looper" / "mcp.json"
    mcp_path.write_text(json.dumps(raw))
    pol = team_policy(["cli:codex"])
    name = pol.team[0]
    from conftest import FakeRunner
    fake = FakeRunner(lambda a, p, n: "ok")
    api.ask(pol, name, "hi", project=str(root), runner=fake, available={name: "/usr/bin/codex"}, timeout=5)
    joined = " ".join(fake.calls[0].argv)
    assert SECRET not in joined
    assert SECRET not in fake.calls[0].prompt
    assert fake.calls[0].extra_env.get("NOTES_TOKEN") == SECRET
    assert SECRET not in mcp_path.read_text(encoding="utf-8")
    assert "${NOTES_TOKEN}" in mcp_path.read_text(encoding="utf-8")

    pol_c = team_policy(["cli:claude"])
    fake_c = FakeRunner(lambda a, p, n: "ok")
    api.ask(pol_c, pol_c.team[0], "hi", project=str(root), runner=fake_c,
            available={pol_c.team[0]: "/usr/bin/claude"}, timeout=5)
    claude_argv = " ".join(fake_c.calls[0].argv)
    assert SECRET not in claude_argv
    assert "--mcp-config" in fake_c.calls[0].argv
    cfg = fake_c.calls[0].argv[fake_c.calls[0].argv.index("--mcp-config") + 1]
    assert oct(os.stat(cfg).st_mode & 0o777) == "0o600"


def _notes_project(root: Path, **extra) -> None:
    (root / ".looper").mkdir(parents=True)
    server = {
        "command": sys.executable,
        "args": ["-u", str(SERVER)],
        "env": {"FAKE_MCP_LOG": str(root / "calls.log")},
    }
    server.update(extra)
    (root / ".looper" / "mcp.json").write_text(json.dumps({"mcpServers": {"notes": server}}))


class _Scripted:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, inv):
        self.calls.append(inv)
        item = self.results.pop(0)
        if isinstance(item, CallResult):
            item.agent = inv.agent.name
            item.argv = inv.argv
            return item
        return CallResult(inv.agent.name, inv.argv, True, str(item), returncode=0)


def _api_team():
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    return pol, pol.team[0], {pol.team[0]: "https://api.anthropic.com"}


def _call(tool: str, args: dict | None = None, cid: str = "call_1") -> CallResult:
    return CallResult(
        "x", [], True, "using a tool",
        tool_calls=[{"id": cid, "tool": tool, "args": args or {}}],
        finish_reason="tool_calls",
    )


def test_read_only_runs_and_write_waits_for_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _notes_project(root)
    log = root / "calls.log"
    pol, name, avail = _api_team()
    scripted = _Scripted([
        _call("mcp__notes__read_note"),
        "read done",
    ])
    res = api.ask(pol, name, "read the note", project=str(root), runner=scripted, available=avail, timeout=5)
    assert res["ok"] and res["finish_reason"] == "stop"
    assert res["text"] == "read done"
    assert res["tool_calls"] == []
    assert "pending" not in res
    assert log.read_text(encoding="utf-8").split() == ["read_note"]
    assert "note-body" in json.dumps(scripted.calls[1].messages)

    log.write_text("")
    scripted = _Scripted([_call("mcp__notes__write_note", {"text": "hello"}, "call_w")])
    pending = api.ask(pol, name, "write it", project=str(root), runner=scripted, available=avail, timeout=5)
    assert pending["finish_reason"] == "approval_required"
    assert pending["tool_calls"] == []
    assert pending["pending"][0]["server"] == "notes"
    assert pending["pending"][0]["name"] == "write_note"
    assert pending["pending"][0]["id"] == "call_w"
    assert pending["pending"][0]["reason"] == "not_read_only"
    assert log.read_text(encoding="utf-8") == ""

    scripted2 = _Scripted(["wrote done"])
    resumed = api.ask(
        pol, name, project=str(root), runner=scripted2, available=avail, timeout=5,
        messages=pending["messages"],
        approvals=[{"id": "call_w", "decision": "approve"}],
    )
    assert resumed["ok"] and resumed["finish_reason"] == "stop"
    assert resumed["text"] == "wrote done"
    assert log.read_text(encoding="utf-8").split() == ["write_note"]
    assert "wrote:hello" in json.dumps(scripted2.calls[0].messages)

    log.write_text("")
    denied = api.ask(
        pol, name, project=str(root), runner=_Scripted(["stopped"]), available=avail, timeout=5,
        messages=pending["messages"],
        approvals=[{"id": "call_w", "decision": "deny", "reason": "no thanks"}],
    )
    assert denied["text"] == "stopped"
    assert log.read_text(encoding="utf-8") == ""
    assert "no thanks" in json.dumps(denied["messages"])


def test_caller_tool_returned_alongside_pending_mcp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _notes_project(root)
    pol, name, avail = _api_team()
    tools = [{
        "name": "write_file",
        "description": "Write a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }]
    res = api.ask(
        pol, name, "both", project=str(root), tools=tools, runner=_Scripted([
            CallResult("x", [], True, "both", tool_calls=[
                {"id": "c_file", "tool": "write_file", "args": {"path": "a.py"}},
                {"id": "c_mcp", "tool": "mcp__notes__write_note", "args": {"text": "x"}},
            ], finish_reason="tool_calls"),
        ]),
        available=avail, timeout=5,
    )
    assert res["finish_reason"] == "approval_required"
    assert [c["tool"] for c in res["tool_calls"]] == ["write_file"]
    assert res["pending"][0]["tool"] == "mcp__notes__write_note"
    assert not (root / "calls.log").exists()


def test_role_require_approval_and_auto_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _notes_project(root)
    (root / ".looper" / "roles").mkdir()
    (root / ".looper" / "roles" / "coder.md").write_text(
        "---\nrequire_approval:\n  - read_note\n---\n\nCareful.\n",
        encoding="utf-8",
    )
    pol, name, avail = _api_team()
    res = api.ask(
        pol, name, "read", role="coder", project=str(root),
        runner=_Scripted([_call("mcp__notes__read_note", cid="call_r")]),
        available=avail, timeout=5,
    )
    assert res["finish_reason"] == "approval_required"
    assert res["pending"][0]["reason"] == "require_approval"
    assert not (root / "calls.log").exists()

    (root / ".looper" / "roles" / "coder.md").write_text(
        "---\nauto_approve:\n  - write_note\n---\n\nTrusted.\n",
        encoding="utf-8",
    )
    (root / "calls.log").write_text("")
    ran = api.ask(
        pol, name, "write", role="coder", project=str(root),
        runner=_Scripted([
            _call("mcp__notes__write_note", {"text": "z"}, "call_z"),
            "done",
        ]),
        available=avail, timeout=5,
    )
    assert ran["finish_reason"] == "stop" and ran["text"] == "done"
    assert (root / "calls.log").read_text(encoding="utf-8").split() == ["write_note"]


def test_deny_does_not_call_the_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _notes_project(root, deny=["read_note"])
    pol, name, avail = _api_team()
    res = api.ask(
        pol, name, "read", project=str(root),
        runner=_Scripted([_call("mcp__notes__read_note"), "refused"]),
        available=avail, timeout=5,
    )
    assert res["text"] == "refused"
    assert res["finish_reason"] == "stop"
    assert not (root / "calls.log").exists() or (root / "calls.log").read_text() == ""
    assert "denied by policy" in json.dumps(res["messages"])


def test_include_filter_hides_write_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _notes_project(root, include=["read_*"])
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    doc = api.doctor(pol, probe=True, project=root)
    tools = doc["mcp"]["servers"][0]["tools"]
    assert doc["mcp"]["servers"][0]["reachable"] is True
    assert [t["name"] for t in tools] == ["read_note"]
    result = runner.invoke(app, ["mcp", "tools", "notes", "--json", "--project", str(root)])
    assert result.exit_code == 0, result.output
    assert [t["name"] for t in json.loads(result.output)["tools"]] == ["read_note"]
    listed = runner.invoke(app, ["mcp", "list", "--json", "--project", str(root), "--no-probe"])
    payload = json.loads(listed.output)
    assert payload["servers"][0]["name"] == "notes"
    assert payload["servers"][0]["reachable"] is None


def test_missing_placeholder_is_reported_not_executed(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    root = tmp_path / "repo"
    (root / ".looper").mkdir(parents=True)
    (root / ".looper" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"notes": {"command": "python", "env": {"T": "${MISSING_MCP_TOKEN}"}}}
    }))
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    doc = api.doctor(pol, probe=False, project=root)
    blob = json.dumps(doc)
    assert "MISSING_MCP_TOKEN" in blob
    assert doc["mcp"]["servers"][0]["error"]
    assert "mcp server 'notes'" in " ".join(doc["problems"])


def test_parse_sse_body_and_http_client():
    raw = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"ping"}]}}\n\n'
    )
    parsed = parse_sse_body(raw)
    assert parsed["result"]["tools"][0]["name"] == "ping"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            method = body.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "h", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{
                    "name": "ping",
                    "description": "ping",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                }]}
            else:
                result = {"content": [{"type": "text", "text": "pong"}], "isError": False}
            payload = ("data: " + json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):  # noqa: A003
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        spec = ServerSpec(name="remote", transport="http", source="test", url=f"http://{host}:{port}/mcp")
        hub = McpHub(timeout=5)
        try:
            tools = hub.list_tools(spec)
            assert [t.name for t in tools] == ["ping"]
            ok, text = hub.call_tool(spec, "ping", {})
            assert ok and text == "pong"
        finally:
            hub.close()
    finally:
        httpd.shutdown()
