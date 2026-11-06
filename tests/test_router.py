import pytest

from looper.config import ConfigError
from looper.router import route


@pytest.mark.parametrize("prompt,expected", [
    ("implement a rate limiter", "explore_plan_code_commit"),
    ("fix the bug in auth.py", "hypothesis_tool_scout"),
    ("add a test for the login flow", "tdd"),
    ("production is down 500 errors", "incident_response"),
    ("research the best vector DB", "deep_research"),
    ("write a spec with acceptance criteria for the billing API", "spec_driven"),
    ("give me several approaches for naming this product", "best_of_n"),
    ("rename the logger in every module across the codebase", "orchestrator_workers"),
    ("simplify this over-engineered build pipeline", "musk_algorithm"),
    ("grade this blog post for AI slop", "editor_slop_grader"),
    ("integrate the Stripe SDK", "research_plan_implement"),
    ("refine and polish the onboarding copy generator", "evaluator_optimizer"),
    ("run a PDCA cycle to reduce build time", "pdca"),
])
def test_routes(policy, prompt, expected):
    d = route(prompt, policy)
    assert d.loop == expected, d.reason
    assert d.method == "rules"
    assert d.score > 0 and d.reason


def test_default_fallback(policy):
    d = route("hello there", policy)
    assert d.loop == "react" and d.method == "default"
    assert "default loop" in d.reason


def test_override(policy):
    d = route("production is down", policy, override="tdd")
    assert (d.loop, d.method) == ("tdd", "override")
    assert d.ranking[0].loop == "incident_response"  # ranking still explained


def test_unknown_override(policy):
    with pytest.raises(ConfigError):
        route("x", policy, override="nope")


def test_ranking_is_explainable(policy):
    d = route("fix the bug in auth.py", policy)
    top = d.ranking[0]
    assert any("bug" in m for m in top.matched)
    assert any("fix" in m for m in top.matched)
