import threading
import time

import pytest

from looper.assign import SMALL_PREFIX, TEXT_ONLY_PREFIX
from looper.config import ConfigError
from looper.executor import Executor

from conftest import FakeRunner, team_available, team_policy

TEAMS = {
    1: ["cli:claude"],
    2: ["cli:claude", "openai:gpt-6.1-sol"],
    3: ["anthropic:claude-opus-5-5", "cli:codex", "ollama:qwen3.5:9b"],
    4: ["cli:claude", "cli:codex", "xai:grok-4.7", "ollama:qwen3.6:27b"],
    5: ["cli:claude", "anthropic:claude-opus-5-5", "cli:codex", "xai:grok-4.7", "ollama:qwen3.6:27b"],
    "local-tiny": ["ollama:qwen3.5:0.8b"],
    "local-small": ["ollama:qwen3.5:4b"],
    "local-medium": ["ollama:qwen3.5:9b"],
    "local-pair": ["ollama:qwen3.5:4b", "lmstudio:qwen3.6-27b"],
    "mixed-small": ["anthropic:claude-haiku-5-5", "ollama:qwen3.5:2b"],
}


def make(specs, runner=None, **kw):
    pol = team_policy(specs)
    return pol, Executor(pol, runner=runner or FakeRunner(), available=team_available(pol), **kw)


@pytest.mark.parametrize("team", list(TEAMS))
def test_every_loop_plans_for_every_team(team):
    pol, ex = make(TEAMS[team])
    for loop in pol.loops:
        plan = ex.plan("do the thing", loop)
        assert plan["stages_without_agent"] == [], (team, loop)
        assert plan["stages"], (team, loop)
        assert all(s["agent"] in pol.team for s in plan["stages"])
        assert plan["team_size"] == len(TEAMS[team])
        for s in plan["stages"]:
            if s["type"] == "parallel":
                assert 1 <= s["fanout"] <= max(len(TEAMS[team]), pol.defaults.single_agent_samples)
        if plan["iterate"]:
            assert plan["iterate"]["max_iterations"] >= 1


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_role_assignment_by_team_size(n):
    pol, ex = make(TEAMS[n])
    plan = ex.plan("implement a rate limiter", "explore_plan_code_commit")
    agents = {s["stage"]: s["agent"] for s in plan["stages"]}
    assert set(agents) == {"explore", "plan", "code", "verify", "commit"}  # frontier teams keep every stage
    if n >= 2:
        assert agents["verify"] != agents["code"]  # reviewer != coder
    assert plan["degradations"] == []
    assert any("diminish" in w for w in plan["warnings"]) == (n > pol.defaults.recommended_agents)


def test_team_over_cap_rejected():
    with pytest.raises(ConfigError, match="max_agents"):
        team_policy(["cli:claude", "cli:codex", "cli:grok", "cli:agy", "ollama:a:1b", "ollama:b:2b"])


def test_lead_roles_go_to_strongest():
    pol, ex = make(["ollama:qwen3.5:9b", "anthropic:claude-sonnet-5-5"])
    plan = ex.plan("x", "orchestrator_workers")
    by = {s["stage"]: s for s in plan["stages"]}
    assert by["orchestrate"]["agent"] == "claude-sonnet-5-5"
    assert by["synthesize"]["agent"] == "claude-sonnet-5-5"


def test_tool_roles_prefer_cli():
    pol, ex = make(["anthropic:claude-opus-5-5", "cli:codex"])
    plan = ex.plan("x", "tdd")
    by = {s["stage"]: s["agent"] for s in plan["stages"]}
    assert by["write_test"] == by["run_tests"] == "codex"
    assert by["refactor"] == "claude-opus-5-5"  # reviewer: no tools needed, diverse from coder


def test_small_local_team_degrades():
    pol, ex = make(TEAMS["local-small"])
    plan = ex.plan("implement a rate limiter", "explore_plan_code_commit")
    names = [s["stage"] for s in plan["stages"]]
    assert names == ["explore", "code", "verify"]
    assert {"stage": "plan", "status": "merged", "merged_into": "explore"} in plan["dropped_stages"]
    assert {"stage": "commit", "status": "skipped", "merged_into": None} in plan["dropped_stages"]
    verify = plan["stages"][2]
    assert verify["status"] == "under_tier"
    assert plan["iterate"]["max_iterations"] == 2  # 3 x 0.67
    text = " ".join(plan["degradations"])
    assert "lite mode" in text and "merged 'plan'" in text and "below recommended tier 'medium'" in text
    assert any("no team member can read files" in w for w in plan["warnings"])
    assert plan["stages"][0]["prompt_preview"].startswith(TEXT_ONLY_PREFIX[:20])


def test_tiny_team_runs_everything_under_tier():
    pol, ex = make(TEAMS["local-tiny"])
    for loop in pol.loops:
        plan = ex.plan("x", loop)
        assert all(s["status"] in ("under_tier", "ok") for s in plan["stages"])
        assert plan["degradations"]
    plan = ex.plan("x", "react")
    assert plan["iterate"]["max_iterations"] == 2  # 6 x 0.34
    assert plan["stages"][0]["context_chars"] <= 6000


def test_best_of_n_adapts():
    _, ex = make(["cli:claude"])
    p = ex.plan("x", "best_of_n")
    assert p["stages"][0]["fanout"] == 1 and "self-critique" in " ".join(p["degradations"])
    _, ex = make(TEAMS["local-medium"])
    p = ex.plan("x", "best_of_n")
    assert p["stages"][0]["fanout"] == 2 and p["stages"][0]["temperatures"] == [0.2, 0.9]
    _, ex = make(TEAMS["local-small"])
    assert ex.plan("x", "best_of_n")["stages"][0]["fanout"] == 1
    _, ex = make(TEAMS[2])
    assert ex.plan("x", "best_of_n")["stages"][0]["fanout"] == 2
    _, ex = make(TEAMS[5])
    s = ex.plan("x", "best_of_n")["stages"][0]
    assert s["fanout"] == 3
    # distinct vendors first: claude CLI and the Anthropic API count as one vendor
    assert s["preference"][:3] == ["claude", "codex", "grok-4.7"]


def test_orchestrator_workers_single_agent_sequential():
    pol, ex = make(["cli:claude"])
    ad = ex.adapt("orchestrator_workers")
    assert ad.loop.stage("workers").max_workers == 1
    assert any("sequentially" in d for d in ad.degradations)
    pol, ex = make(TEAMS["local-medium"])
    ad = ex.adapt("orchestrator_workers")
    w = ad.loop.stage("workers")
    assert w.max_workers == 1 and w.max_items <= 3  # medium can't orchestrate -> under-tier source


def test_max_iterations_override_wins():
    _, ex = make(TEAMS["local-tiny"], max_iterations=4)
    assert ex.plan("x", "tdd")["iterate"]["max_iterations"] == 4


def test_small_team_execution_merges_and_sets_limits():
    seen = []

    def responder(agent, prompt, n):
        seen.append(prompt)
        return "LGTM" if "Review" in prompt or "review" in prompt else f"out{n}"
    r = FakeRunner(responder)
    pol, ex = make(TEAMS["local-small"], runner=r)
    res = ex.run("implement a rate limiter", "explore_plan_code_commit", write_transcript=False)
    assert res.status == "ok", res.stop_reason
    assert [s.stage for s in res.stages][:2] == ["explore", "code"]
    inv = r.calls[0]
    assert inv.max_tokens == 4096 and inv.num_ctx == 8192 and inv.disable_thinking
    assert SMALL_PREFIX in inv.prompt and TEXT_ONLY_PREFIX in inv.prompt
    assert "THEN, in the same answer" in inv.prompt  # plan merged into explore
    assert "out1" in r.calls[1].prompt  # code stage sees explore(+plan) output via alias
    assert res.degradations and res.team[0]["tier"] == "small"


def test_local_calls_are_serialized():
    active, peak, lock = [0], [0], threading.Lock()

    def responder(agent, prompt, n):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return "x"
    _, ex = make(["ollama:qwen3.6:27b"], runner=FakeRunner(responder))
    res = ex.run("x", "best_of_n", write_transcript=False)
    assert res.status == "ok" and len(res.stages[0].calls) == 2
    assert peak[0] == 1


def test_temperatures_passed_for_single_model_sampling():
    r = FakeRunner()
    _, ex = make(TEAMS["local-medium"], runner=r)
    ex.run("x", "best_of_n", write_transcript=False)
    assert sorted(c.temperature for c in r.calls[:2]) == [0.2, 0.9]
    assert r.calls[-1].temperature is None


def test_unavailable_api_member_is_reported(monkeypatch):
    pol = team_policy(["anthropic:claude-sonnet-5-5", "ollama:qwen3.5:9b"])
    ex = Executor(pol, runner=FakeRunner())  # default availability: no ANTHROPIC_API_KEY set
    plan = ex.plan("x", "react")
    assert "claude-sonnet-5-5" in plan["unavailable_agents"]
    assert plan["available_agents"] == ["qwen3.5-9b"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-000000000000")
    ex = Executor(pol, runner=FakeRunner())
    assert ex.plan("x", "react")["stages"][0]["agent"] == "claude-sonnet-5-5"
