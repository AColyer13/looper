import json
import os
import stat

import pytest
from typer.testing import CliRunner

from looper.cli import app
from looper.policy import load_policy
from looper.setup import Choice, enrich_local, needed_keys, parse_agent_spec

runner = CliRunner()
KEY = "sk-ant-secret-0123456789-LAST"


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "lh"
    monkeypatch.setenv("LOOPER_HOME", str(h))
    return h


def test_parse_agent_spec(policy):
    c = parse_agent_spec("ollama:qwen3.5:9b", policy)
    assert (c.name, c.kind, c.entry) == ("qwen3.5-9b", "local",
                                         {"kind": "local", "provider": "ollama", "model": "qwen3.5:9b"})
    c = parse_agent_spec("cli:codex", policy)
    assert c.name == "codex" and c.entry is None
    assert parse_agent_spec("claude", policy).name == "claude"
    c = parse_agent_spec("openai_compatible:Qwen/Qwen3.6-27B@http://gpu:8000/v1#large", policy)
    assert c.entry == {"kind": "local", "provider": "openai_compatible", "model": "Qwen/Qwen3.6-27B",
                       "base_url": "http://gpu:8000/v1", "tier": "large"}
    assert c.name == "openai_compatible-Qwen3.6-27B"
    for bad in ("nope:x", "cli:gpt", "anthropic:", "ollama:x#huge", "plain"):
        with pytest.raises(ValueError):
            parse_agent_spec(bad, policy)


def test_needed_keys_one_per_provider(policy):
    cs = [parse_agent_spec(s, policy) for s in
          ("anthropic:claude-opus-5-5", "anthropic:claude-haiku-5-5", "xai:grok-4.7", "ollama:qwen3.5:4b",
           "cli:claude", "lmstudio:foo")]
    assert needed_keys(cs, policy) == {"ANTHROPIC_API_KEY": ["anthropic"], "XAI_API_KEY": ["xai"]}


def test_enrich_local_from_ollama(policy):
    from test_tiers_local import SHOW_MOE, fake_transport
    t = fake_transport({("GET", "api/tags"): {"models": [{"name": "qwen3:30b-a3b"}]},
                        ("POST", "api/show"): SHOW_MOE})
    c = enrich_local(parse_agent_spec("ollama:qwen3:30b-a3b", policy), policy, transport=t)
    assert c.entry["context_window"] == 262144 and c.entry["active_params_b"] == 3
    assert c.meta["probed"] == "api/show"


def test_setup_non_interactive(home, tmp_path):
    cfg = tmp_path / "proj" / "looper.json"
    r = runner.invoke(app, ["setup", "--agent", "cli:claude", "--agent", "anthropic:claude-sonnet-5-5",
                            "--agent", "ollama:qwen3.5:4b", "--path", str(cfg), "--no-probe", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert [m["name"] for m in out["team"]] == ["claude", "claude-sonnet-5-5", "qwen3.5-4b"]
    assert [m["tier"] for m in out["team"]] == ["frontier", "frontier", "small"]
    assert out["keys"] == {"ANTHROPIC_API_KEY": "missing"}
    data = json.loads(cfg.read_text())
    assert data["team"] == ["claude", "claude-sonnet-5-5", "qwen3.5-4b"]
    assert "api_key" not in cfg.read_text().lower().replace("api_key_env", "")
    pol = load_policy(cfg)
    assert pol.team == data["team"] and pol.agents["qwen3.5-4b"].base_url == "http://localhost:11434"
    # re-run merges: existing keys preserved, team replaced
    data["router"] = {"min_score": 3}
    cfg.write_text(json.dumps(data))
    r = runner.invoke(app, ["setup", "-a", "cli:codex", "--path", str(cfg), "--no-probe"])
    assert r.exit_code == 0
    data = json.loads(cfg.read_text())
    assert data["team"] == ["codex"] and data["router"] == {"min_score": 3}


def test_setup_limits(home, tmp_path):
    cfg = str(tmp_path / "l.json")
    six = sum([["-a", s] for s in ("cli:claude", "cli:codex", "cli:grok", "cli:agy", "ollama:a:1b", "ollama:b:2b")], [])
    r = runner.invoke(app, ["setup", *six, "--path", cfg, "--no-probe"])
    assert r.exit_code == 2 and "max of 5" in r.output
    r = runner.invoke(app, ["setup", "-a", "bogus:x", "--path", cfg, "--no-probe"])
    assert r.exit_code == 2 and "unknown provider" in r.output
    r = runner.invoke(app, ["setup", *six[:8], "--path", cfg, "--no-probe"])
    assert r.exit_code == 0 and "sweet spot" in r.output


def test_setup_prompt_keys_after_selection(home, tmp_path):
    cfg = tmp_path / "k.json"
    r = runner.invoke(app, ["setup", "-a", "anthropic:claude-opus-5-5", "-a", "xai:grok-4.7", "--path", str(cfg),
                            "--no-probe", "--prompt-keys"], input=KEY + "\n\n")  # 2nd key skipped
    assert r.exit_code == 0, r.output
    assert KEY not in r.output and KEY not in cfg.read_text()
    p = home / "credentials.env"
    assert KEY in p.read_text() and stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert "XAI_API_KEY: skipped" in r.output.replace("$", "")


def test_setup_interactive_wizard(home, tmp_path, monkeypatch):
    monkeypatch.setattr("looper.local.discover_all", lambda **kw: [])
    cfg = tmp_path / "w.json"
    # 2 agents: menu item 1 (cli:claude) then a typed spec; then the hidden key prompt
    r = runner.invoke(app, ["setup", "--path", str(cfg), "--no-probe"],
                      input="2\n1\nanthropic:claude-sonnet-5-5\n" + KEY + "\n")
    assert r.exit_code == 0, r.output
    assert r.output.index("How many agents") < r.output.index("API key for $ANTHROPIC_API_KEY")
    assert KEY not in r.output
    assert json.loads(cfg.read_text())["team"] == ["claude", "claude-sonnet-5-5"]
    assert KEY in (home / "credentials.env").read_text()


def test_models_agents_spec_commands(home, tmp_path, monkeypatch):
    cfg = tmp_path / "m.json"
    runner.invoke(app, ["setup", "-a", "cli:claude", "-a", "ollama:qwen3.5:0.8b", "--path", str(cfg), "--no-probe"])
    r = runner.invoke(app, ["models", "--config", str(cfg), "--json"])
    rows = {x["name"]: x for x in json.loads(r.output)["team"]}
    assert rows["qwen3.5-0.8b"]["tier"] == "tiny" and "planner" not in rows["qwen3.5-0.8b"]["roles"]
    assert rows["claude"]["tier"] == "frontier"
    r = runner.invoke(app, ["agents", "--config", str(cfg), "--json", "--no-probe"])
    data = json.loads(r.output)
    assert data["qwen3.5-0.8b"]["tier"]["tier"] == "tiny" and data["qwen3.5-0.8b"]["in_team"]
    r = runner.invoke(app, ["spec"])
    spec = json.loads(r.output)
    assert set(spec["tiers"]) == {"tiny", "small", "medium", "large", "xl", "frontier"}
    assert spec["team_limits"]["max_agents"] == 5 and spec["assignment_algorithm"]
    r = runner.invoke(app, ["--config", str(cfg), "--loop", "best_of_n", "--dry-run", "name my app"])
    assert r.exit_code == 0 and "adapted to this team" in r.output


def test_team_spec_doc_in_sync(policy):
    from pathlib import Path
    from looper.spec import build_spec
    doc = Path(__file__).resolve().parents[1] / "docs" / "team_spec.json"
    assert json.loads(doc.read_text()) == json.loads(json.dumps(build_spec(policy)))
