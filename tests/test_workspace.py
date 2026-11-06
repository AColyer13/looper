"""Workspace loader, skill override, load_skill, and prompt injection."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from conftest import FakeRunner, team_available, team_policy
from looper import api
from looper.cli import app
from looper.config import find_config
from looper.executor import Executor
from looper.policy import load_policy
from looper.runners import CallResult
from looper.workspace import load_workspace, parse_frontmatter

runner = CliRunner()


def _skill(directory: Path, name: str, description: str, body: str) -> None:
    folder = directory / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _project(root: Path, **files: str) -> Path:
    looper = root / ".looper"
    looper.mkdir(parents=True)
    for name, text in files.items():
        path = looper / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_frontmatter_lists_and_scalars():
    meta, body = parse_frontmatter(
        "---\nmodel: grok\nmax_tool_calls: 2\ndeny:\n  - write_note\n"
        "auto_approve: [read_note, \"notes:peek\"]\n---\n\nBe brief.\n"
    )
    assert meta["model"] == "grok"
    assert meta["max_tool_calls"] == 2
    assert meta["deny"] == ["write_note"]
    assert meta["auto_approve"] == ["read_note", "notes:peek"]
    assert body == "Be brief."


def test_project_skill_overrides_machine_skill(tmp_path, monkeypatch):
    machine = Path.home() / ".looper"
    _skill(machine, "ship", "machine description", "MACHINE_BODY_SENTINEL")
    root = tmp_path / "proj"
    _project(root)
    _skill(root / ".looper", "ship", "project description", "PROJECT_BODY_SENTINEL")
    _skill(machine, "only-user", "user only", "USER_ONLY_BODY")
    ws = load_workspace(start=root, project=root)
    assert ws.skills["ship"].description == "project description"
    assert ws.skills["ship"].scope == "project"
    assert "PROJECT_BODY_SENTINEL" in ws.skills["ship"].body
    assert "MACHINE_BODY_SENTINEL" not in ws.skills["ship"].body
    assert ws.skills["only-user"].scope == "user"


def test_walk_up_finds_project_and_override_wins(tmp_path):
    root = tmp_path / "repo"
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    _project(root, **{"AGENTS.md": "RULES_SENTINEL"})
    found = load_workspace(start=nested)
    assert found.project_root == root.resolve()
    assert "RULES_SENTINEL" in (found.agents_md or "")
    other = tmp_path / "other"
    other.mkdir()
    _project(other, **{"AGENTS.md": "OTHER_SENTINEL"})
    forced = load_workspace(start=nested, project=other)
    assert forced.project_root == other.resolve()
    assert "OTHER_SENTINEL" in (forced.agents_md or "")


def test_state_home_config_precedes_legacy_user_config(tmp_path, monkeypatch):
    state = Path.home() / ".looper"
    state.mkdir(parents=True)
    (state / "looper.json").write_text(json.dumps({"router": {"default_loop": "tdd"}}))
    user = tmp_path / "xdg" / "looper"
    user.mkdir(parents=True)
    (user / "looper.json").write_text(json.dumps({"router": {"default_loop": "react"}}))
    assert find_config()[0] == "~/.looper"
    assert load_policy().default_loop == "tdd"


def test_no_workspace_cli_prompt_is_unchanged():
    pol = team_policy(["cli:codex"])
    name = pol.team[0]
    fake = FakeRunner(lambda a, p, n: "hello")
    res = api.ask(pol, name, "hi", runner=fake, available=team_available(pol))
    assert res["ok"] and res["text"] == "hello" and res["finish_reason"] == "stop"
    assert fake.calls[0].prompt == "hi"
    assert "--append-system-prompt-file" not in fake.calls[0].argv
    assert "project_doc_fallback_filenames" not in " ".join(fake.calls[0].argv)


def test_agents_and_role_injected_for_api_soul_only_without_persona():
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(
        root,
        **{
            "AGENTS.md": "AGENTS_SENTINEL follow these rules",
            "SOUL.md": "SOUL_SENTINEL you are a gardener",
            "USER.md": "USER_SENTINEL likes short answers",
            "roles/coder.md": "---\nmodel: grok\nmax_tool_calls: 1\n---\n\nROLE_SENTINEL write tests first\n",
        },
    )
    seen = {}

    def respond(agent, prompt, n):
        seen["prompt"] = prompt
        return "ok"

    fake = FakeRunner(respond)
    res = api.ask(
        pol, name, "plan the change", role="coder", project=str(root),
        runner=fake, available={name: "https://api.anthropic.com"},
    )
    assert res["ok"]
    system = fake.calls[0].messages[0]
    assert system["role"] == "system"
    text = system["content"]
    assert "AGENTS_SENTINEL" in text
    assert "SOUL_SENTINEL" in text
    assert "USER_SENTINEL" in text
    assert "ROLE_SENTINEL" in text
    assert "Operating rules" in text and "Role: coder" in text

    fake2 = FakeRunner(lambda a, p, n: "ok")
    api.ask(
        pol, name, "plan", system="CALLER_PERSONA", project=str(root),
        runner=fake2, available={name: "https://api.anthropic.com"},
    )
    persona = fake2.calls[0].messages[0]["content"]
    assert persona.startswith("CALLER_PERSONA")
    assert "SOUL_SENTINEL" not in persona
    assert "AGENTS_SENTINEL" in persona


def test_load_skill_executes_inside_the_turn_and_hides_the_body():
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root)
    _skill(root / ".looper", "ship", "Ship a change", "SKILL_BODY_SENTINEL do the release checklist")
    calls = []

    class Scripted:
        def __call__(self, inv):
            calls.append(inv)
            if len(calls) == 1:
                return CallResult(
                    inv.agent.name, inv.argv, True, "loading",
                    tool_calls=[{
                        "id": "call_skill",
                        "tool": "load_skill",
                        "args": {"name": "ship"},
                    }],
                    finish_reason="tool_calls",
                )
            return CallResult(inv.agent.name, inv.argv, True, "shipped", finish_reason="stop")

    res = api.ask(
        pol, name, "ship it", project=str(root),
        runner=Scripted(), available={name: "https://api.anthropic.com"},
    )
    assert res["ok"] and res["finish_reason"] == "stop"
    assert res["text"] == "shipped"
    assert res["tool_calls"] == []
    first_system = calls[0].messages[0]["content"]
    assert "ship: Ship a change" in first_system or "- ship: Ship a change" in first_system
    assert "SKILL_BODY_SENTINEL" not in first_system
    second = json.dumps(calls[1].messages)
    assert "SKILL_BODY_SENTINEL" in second
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_skill" for m in res["messages"])


def test_caller_tool_still_returned_when_load_skill_runs():
    pol = team_policy(["anthropic:claude-sonnet-5-5"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root)
    _skill(root / ".looper", "ship", "Ship a change", "SKILL_BODY_SENTINEL")
    tools = [{
        "name": "write_file",
        "description": "Write",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }]

    class Scripted:
        def __call__(self, inv):
            return CallResult(
                inv.agent.name, inv.argv, True, "both",
                tool_calls=[
                    {"id": "call_skill", "tool": "load_skill", "args": {"name": "ship"}},
                    {"id": "call_write", "tool": "write_file", "args": {"path": "a.py"}},
                ],
                finish_reason="tool_calls",
            )

    res = api.ask(
        pol, name, "do both", project=str(root), tools=tools,
        runner=Scripted(), available={name: "https://api.anthropic.com"},
    )
    assert res["finish_reason"] == "tool_calls"
    assert [c["tool"] for c in res["tool_calls"]] == ["write_file"]
    assert res["tool_calls"][0]["id"] == "call_write"
    assert "pending" not in res
    bodies = [m.get("content") for m in res["messages"] if m.get("role") == "tool"]
    assert any("SKILL_BODY_SENTINEL" in (b or "") for b in bodies)


def test_claude_gets_agents_file_not_a_copy_in_the_prompt():
    pol = team_policy(["cli:claude"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root, **{"AGENTS.md": "AGENTS_SENTINEL do not duplicate"})
    fake = FakeRunner(lambda a, p, n: "ok")
    api.ask(pol, name, "look", project=str(root), runner=fake, available={name: "/usr/bin/claude"})
    argv = fake.calls[0].argv
    assert "--append-system-prompt-file" in argv
    flag = argv[argv.index("--append-system-prompt-file") + 1]
    assert flag.endswith("AGENTS.md")
    assert "AGENTS_SENTINEL" not in fake.calls[0].prompt


def test_codex_gets_native_agents_hook():
    pol = team_policy(["cli:codex"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root, **{"AGENTS.md": "AGENTS_SENTINEL codex"})
    fake = FakeRunner(lambda a, p, n: "ok")
    api.ask(pol, name, "look", project=str(root), runner=fake, available={name: "/usr/bin/codex"})
    joined = " ".join(fake.calls[0].argv)
    assert "project_doc_fallback_filenames" in joined
    assert ".looper/AGENTS.md" in joined
    assert "AGENTS_SENTINEL" not in fake.calls[0].prompt


def test_grok_has_no_native_hook_so_agents_go_in_the_prompt():
    pol = team_policy(["cli:grok"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root, **{"AGENTS.md": "AGENTS_SENTINEL grok"})
    fake = FakeRunner(lambda a, p, n: "ok")
    api.ask(pol, name, "look", project=str(root), runner=fake, available={name: "/usr/bin/grok"})
    assert "AGENTS_SENTINEL" in fake.calls[0].prompt
    assert "--mcp-config" not in fake.calls[0].argv
    assert "--append-system-prompt-file" not in fake.calls[0].argv


def test_role_model_is_preferred_for_that_stage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, **{"roles/coder.md": "---\nmodel: grok\n---\n\nPrefer grok for code.\n"})
    pol = load_policy()
    ex = Executor(pol, available={"claude": "/bin/claude", "codex": "/bin/codex", "grok": "/bin/grok", "agy": "/bin/agy"})
    prefs = ex.preference(pol.loops["tdd"].stage("implement"))
    assert prefs[0] == "grok"


def test_run_prompt_includes_agents_and_role(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(
        tmp_path,
        **{
            "AGENTS.md": "AGENTS_SENTINEL run",
            "roles/coder.md": "ROLE_SENTINEL implement\n",
        },
    )
    pol = load_policy()
    fake = FakeRunner(lambda a, p, n: "ALL TESTS PASS")
    ex = Executor(pol, runner=fake, available={"claude": "/c", "codex": "/c", "grok": "/g", "agy": "/a"},
                  force_agent="codex", max_iterations=1)
    from looper.router import route as route_prompt
    decision = route_prompt("add a test", pol, override="tdd")
    ex.run("add a test", "tdd", decision, write_transcript=False)
    implement = next(c for c in fake.calls if "ROLE_SENTINEL" in c.prompt)
    # Codex loads AGENTS.md itself, so the body is not copied into the prompt.
    assert "AGENTS_SENTINEL" not in implement.prompt
    assert "ROLE_SENTINEL" in implement.prompt
    assert any("project_doc_fallback_filenames" in " ".join(c.argv) for c in fake.calls)


def test_tiny_tier_gets_no_skill_tool():
    pol = team_policy(["ollama:qwen3.5:0.8b#tiny"])
    name = pol.team[0]
    root = Path.cwd() / "repo"
    _project(root)
    _skill(root / ".looper", "ship", "Ship a change", "SKILL_BODY_SENTINEL")
    fake = FakeRunner(lambda a, p, n: "plain")
    res = api.ask(
        pol, name, "ship it", project=str(root),
        tools=[{
            "name": "write_file",
            "description": "Write",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }],
        runner=fake, available={name: "http://127.0.0.1:9"},
    )
    assert res["protocol"] == "none"
    assert res["tool_calls"] == []
    blob = json.dumps(fake.calls[0].messages)
    assert "load_skill" not in blob
    assert "SKILL_BODY_SENTINEL" not in blob
    assert any("tiny" in w for w in res["warnings"])


def test_skills_list_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _project(root)
    _skill(root / ".looper", "ship", "Ship a change", "body")
    result = runner.invoke(app, ["skills", "list", "--json", "--project", str(root)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["skills"][0]["name"] == "ship"
    assert data["skills"][0]["scope"] == "project"


def test_skills_disabled_in_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = Path.home() / ".looper"
    state.mkdir(parents=True)
    (state / "looper.json").write_text(json.dumps({"skills": {"enabled": False}}))
    _skill(state, "ship", "Ship", "BODY")
    ws = load_workspace(start=tmp_path, policy_raw={"skills": {"enabled": False}})
    assert ws.skills == {}
    assert ws.skills_enabled is False
