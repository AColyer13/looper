import json
from pathlib import Path

import pytest

from looper.executor import Budget, CapExceeded, Executor, parse_items, render
from looper.policy import build_policy
from looper.router import route
from looper.runners import CallResult, build_argv

from conftest import ALL, FakeRunner


def ex(policy, runner, available=ALL, **kw):
    return Executor(policy, runner=runner, available=dict(available), **kw)


def test_template_render():
    out = render("T={{task}} P={{outputs.plan}} X={{outputs.none}} I={{ iteration }}",
                 {"task": "t", "iteration": 2, "outputs": {"plan": "p"}})
    assert out == "T=t P=p X=(none yet) I=2"


def test_parse_items_json_and_bullets():
    assert parse_items('Here:\n["a", "b", "c"]', 2) == ["a", "b"]
    assert parse_items("1. one\n2) two\n- three", 10) == ["one", "two", "three"]
    assert parse_items("just text", 3) == ["just text"]


def test_build_argv(policy):
    argv = build_argv(policy.agents["codex"], "/bin/codex", "do it")
    assert argv == ["/bin/codex", "exec", "--skip-git-repo-check", "do it"]
    assert build_argv(policy.agents["claude"], "/c", "hi") == ["/c", "-p", "hi"]


def test_tdd_exit_signal_stops_early(policy, tmp_path):
    def responder(agent, prompt, n):
        if "Run the full relevant test suite" in prompt:
            return "2 passed\nALL TESTS PASS" if n >= 5 else "1 failed"
        return "ok"
    r = FakeRunner(responder)
    res = ex(policy, r, cwd=tmp_path).run("add a test for login", "tdd")
    assert res.status == "ok" and res.exit_signal == "ALL TESTS PASS"
    assert res.iterations == 2  # write_test, (impl, run) x2, refactor
    assert [s.stage for s in res.stages] == ["write_test", "implement", "run_tests", "implement", "run_tests", "refactor"]
    assert len(r.calls) == 6
    # transcript written
    data = json.loads(Path(res.transcript_path).read_text())
    assert data["loop"] == "tdd" and Path(res.transcript_path).parent == tmp_path / ".looper" / "runs"


def test_feedback_flows_between_stages(policy, tmp_path):
    def responder(agent, prompt, n):
        return "PLAN-XYZ" if "implementation plan" in prompt else "LGTM"
    r = FakeRunner(responder)
    ex(policy, r, cwd=tmp_path).run("implement a rate limiter", "explore_plan_code_commit")
    code_prompt = next(c.prompt for c in r.calls if "Implement the plan" in c.prompt)
    assert "PLAN-XYZ" in code_prompt and "implement a rate limiter" in code_prompt


def test_max_iterations_without_signal(policy, tmp_path):
    r = FakeRunner(lambda a, p, n: "still failing")
    res = ex(policy, r, cwd=tmp_path, max_iterations=2).run("t", "tdd", write_transcript=False)
    assert res.iterations == 2 and res.exit_signal is None
    assert "max_iterations=2" in res.stop_reason
    assert len(r.calls) == 1 + 2 * 2 + 1


def test_regex_exit_signal(policy):
    def responder(agent, prompt, n):
        return "SLOP SCORE: 1" if "Re-grade" in prompt else "SLOP SCORE: 7"
    res = ex(policy, FakeRunner(responder)).run("edit my essay", "editor_slop_grader", write_transcript=False)
    assert res.iterations == 1 and res.exit_signal.startswith("re:")
    # final output is the edited artifact, not the grade
    assert res.final_output == "SLOP SCORE: 7"


def test_call_cap_enforced(raw):
    raw["loops"]["tdd"]["caps"] = {"max_total_calls": 3}
    pol = build_policy(raw)
    r = FakeRunner(lambda a, p, n: "nope")
    res = ex(pol, r).run("t", "tdd", write_transcript=False)
    assert res.status == "capped" and "max_total_calls=3" in res.stop_reason
    assert len(r.calls) == 3 and res.calls_made == 3


def test_wall_cap_enforced(policy):
    t = [0.0]

    def clock():
        return t[0]

    def responder(a, p, n):
        t[0] += 1000  # each call "takes" 1000s
        return "x"
    res = ex(policy, FakeRunner(responder), clock=clock).run("t", "react", write_transcript=False)
    assert res.status == "capped" and "max_wall_seconds" in res.stop_reason
    assert res.calls_made == 2  # react cap is 1800s


def test_budget_unit():
    b = Budget(1, 100, clock=lambda: 0.0)
    b.take()
    with pytest.raises(CapExceeded):
        b.take()


def test_fallback_to_next_installed_agent(policy):
    # tdd write_test role=tester -> codex, claude. codex not installed -> claude.
    avail = {k: v for k, v in ALL.items() if k != "codex"}
    r = FakeRunner(lambda a, p, n: "ALL TESTS PASS")
    e = ex(policy, r, available=avail)
    assert e.candidates(policy.loops["tdd"].stage("write_test"))[0] == "claude"
    e.run("t", "tdd", write_transcript=False)
    assert "codex" not in r.agents


def test_fallback_on_error(policy):
    def responder(agent, prompt, n):
        if agent == "claude":
            return CallResult(agent, [], False, "", returncode=1, error="boom")
        return "FINAL ANSWER: 42"
    r = FakeRunner(responder)
    res = ex(policy, r).run("what is 6*7", "react", write_transcript=False)
    assert r.agents[:2] == ["claude", "codex"]
    assert res.status == "ok" and "42" in res.final_output


def test_no_installed_agent_fails_cleanly(policy):
    res = ex(policy, FakeRunner(), available={}).run("t", "react", write_transcript=False)
    assert res.status == "failed" and "no installed agent" in res.stop_reason


def test_force_agent(policy):
    r = FakeRunner(lambda a, p, n: "LGTM ALL TESTS PASS")
    ex(policy, r, force_agent="grok").run("t", "tdd", write_transcript=False)
    assert set(r.agents) == {"grok"}


def test_force_unknown_agent(policy):
    with pytest.raises(ValueError):
        ex(policy, FakeRunner(), force_agent="gpt")


def test_best_of_n_fans_out_across_agents(policy):
    r = FakeRunner(lambda a, p, n: f"solution from {a}")
    res = ex(policy, r).run("name my app", "best_of_n", write_transcript=False)
    cand = res.stages[0]
    assert cand.type == "parallel" and len(cand.calls) == 3
    assert {c["agent"] for c in cand.calls} == {"claude", "codex", "agy"}  # rotation over preference
    judge_prompt = r.calls[-1].prompt
    assert judge_prompt.count("### Candidate") == 3
    assert res.stages[-1].stage == "judge"


def test_orchestrator_workers_maps_subtasks(policy):
    def responder(agent, prompt, n):
        if "Split this task" in prompt:
            return '["update api", "update cli", "update docs"]'
        return "done"
    r = FakeRunner(responder)
    res = ex(policy, r).run("rename foo everywhere", "orchestrator_workers", write_transcript=False)
    workers = res.stages[1]
    assert workers.items == ["update api", "update cli", "update docs"]
    assert len(workers.calls) == 3
    assert any("update cli" in c.prompt for c in r.calls)


def test_dry_run_plan_executes_nothing(policy):
    r = FakeRunner()
    d = route("add a test for the login flow", policy)
    plan = ex(policy, r, available={"claude": "/x/claude"}).plan("add a test for the login flow", d.loop, d)
    assert r.calls == []
    assert plan["loop"] == "tdd"
    assert [s["stage"] for s in plan["stages"]] == ["write_test", "implement", "run_tests", "refactor"]
    assert all(s["agent"] == "claude" for s in plan["stages"])
    assert plan["stages"][0]["argv"][0] == "/x/claude"
    assert plan["iterate"]["exit_signals"] == ["ALL TESTS PASS"]
    assert plan["worst_case_calls"] == 1 + 5 * 2 + 1


def test_dry_run_flags_missing_agents(policy):
    plan = ex(policy, FakeRunner(), available={}).plan("x", "react")
    assert plan["stages_without_agent"] == ["step"]
