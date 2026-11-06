import json
from pathlib import Path

import pytest

from looper.config import ConfigError, find_config
from looper.policy import build_policy, load_policy

PLAYBOOK = {
    "react", "explore_plan_code_commit", "research_plan_implement", "spec_driven", "evaluator_optimizer",
    "best_of_n", "orchestrator_workers", "pdca", "tdd", "incident_response", "musk_algorithm",
    "deep_research", "hypothesis_tool_scout", "editor_slop_grader",
}


def test_bundled_default_loads(policy):
    assert policy.source == "bundled"
    assert set(policy.agents) == {"claude", "codex", "grok", "agy"}
    assert policy.default_loop == "react"


def test_all_14_playbook_loops_present_and_valid(policy):
    assert PLAYBOOK <= set(policy.loops)
    assert len(policy.loops) == 14
    for name, loop in policy.loops.items():
        assert loop.description, name
        assert loop.stages, name
        assert loop.route, f"{name} has no router rules"
        assert loop.caps.max_total_calls > 0 and loop.caps.max_wall_seconds > 0
        for s in loop.stages:
            assert policy.stage_preferences(s), f"{name}.{s.name} has no agent preference"
            assert "{{task}}" in s.prompt
        if loop.iterate:
            assert loop.iterate.exit_signals and loop.iterate.max_iterations >= 1


def test_example_file_matches_bundled():
    root = Path(__file__).resolve().parents[1]
    bundled = root / "src/looper/data/default_config.json"
    example = root / "looper.example.json"
    assert json.loads(example.read_text()) == json.loads(bundled.read_text())


def test_musk_algorithm_has_five_steps(policy):
    assert [s.name for s in policy.loops["musk_algorithm"].stages] == [
        "question_requirements", "delete", "simplify", "accelerate", "automate"]


def test_search_order(tmp_path, monkeypatch):
    assert find_config() == ("bundled", None)
    user = tmp_path / "xdg" / "looper" / "looper.json"
    user.parent.mkdir(parents=True)
    user.write_text("{}")
    assert find_config()[0] == "user"
    (tmp_path / "looper.json").write_text("{}")
    assert find_config()[0] == "./looper.json"
    env = tmp_path / "env.json"
    env.write_text("{}")
    monkeypatch.setenv("LOOPER_CONFIG", str(env))
    assert find_config()[0] == "$LOOPER_CONFIG"
    flag = tmp_path / "flag.json"
    flag.write_text("{}")
    assert find_config(flag) == ("--config", flag)


def test_missing_explicit_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_policy(tmp_path / "nope.json")


def test_user_config_merges_over_defaults(tmp_path):
    cfg = tmp_path / "looper.json"
    cfg.write_text(json.dumps({
        "agents": {"grok": {"binary": "/opt/grok/bin/grok"}},
        "strategies": {"coder": ["codex"]},
        "router": {"default_loop": "tdd"},
        "loops": {"musk_algorithm": None},
    }))
    pol = load_policy()
    assert pol.source == "./looper.json"
    assert pol.agents["grok"].binary == "/opt/grok/bin/grok"
    assert pol.agents["grok"].command[0] == "{binary}"  # untouched keys kept
    assert pol.strategies["coder"] == ["codex"]
    assert pol.default_loop == "tdd"
    assert "musk_algorithm" not in pol.loops
    assert "react" in pol.loops


def test_inherit_defaults_false(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "inherit_defaults": False,
        "agents": {"claude": {"command": ["{binary}", "-p", "{prompt}"]}},
        "strategies": {"x": ["claude"]},
        "router": {"default_loop": "solo"},
        "loops": {"solo": {"description": "d", "stages": [{"name": "s", "role": "x", "prompt": "{{task}}"}]}},
    }))
    pol = load_policy(cfg)
    assert list(pol.loops) == ["solo"]


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r["router"].update(default_loop="ghost"), "default_loop"),
    (lambda r: r["loops"]["tdd"]["stages"][0].update(agents=["nobody"]), "unknown agent 'nobody'"),
    (lambda r: r["loops"]["tdd"]["iterate"].update(body=["implement", "refactor"]), "contiguous"),
    (lambda r: r["loops"]["tdd"]["iterate"].update(check_stages=["write_test"]), "not in body"),
    (lambda r: r["loops"]["tdd"]["stages"][0].update(type="weird"), "type must be"),
    (lambda r: r["loops"]["tdd"]["route"].append({"regex": "(", "weight": 1}), "bad regex"),
    (lambda r: r["agents"]["claude"].update(command=["claude", "-p"]), "{prompt}"),
    (lambda r: r["loops"]["orchestrator_workers"]["stages"][1].update(source="later"), "map stage"),
])
def test_validation_errors(raw, mutate, needle):
    mutate(raw)
    with pytest.raises(ConfigError) as ei:
        build_policy(raw)
    assert needle in str(ei.value)


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_policy(p)
