from __future__ import annotations

import threading
from typing import Callable

import pytest

from looper.policy import Policy, build_policy, load_policy
from looper.config import bundled_default
from looper.runners import CallResult, Invocation


class FakeRunner:
    """Records invocations; ``responder(agent, prompt, n)`` returns output or raises/returns CallResult."""

    def __init__(self, responder: Callable[[str, str, int], object] | None = None):
        self.calls: list[Invocation] = []
        self.responder = responder or (lambda agent, prompt, n: f"{agent} output #{n}")
        self._lock = threading.Lock()

    def __call__(self, inv: Invocation) -> CallResult:
        with self._lock:
            self.calls.append(inv)
            n = len(self.calls)
        r = self.responder(inv.agent.name, inv.prompt, n)
        if isinstance(r, CallResult):
            return r
        return CallResult(inv.agent.name, inv.argv, True, str(r), returncode=0)

    @property
    def agents(self) -> list[str]:
        return [c.agent.name for c in self.calls]


ALL = {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex", "grok": "/usr/bin/grok", "agy": "/usr/bin/agy"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Never pick up a real looper.json / user config during tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOPER_CONFIG", raising=False)
    monkeypatch.delenv("LOOPER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                "LM_API_TOKEN", "LOOPER_COMPAT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("looper.credentials._keyring", lambda: None)  # never touch a real OS keyring
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def policy() -> Policy:
    return load_policy()


@pytest.fixture
def raw() -> dict:
    return bundled_default()


def make_policy(raw: dict) -> Policy:
    return build_policy(raw)


def team_policy(specs: list[str], **defaults) -> Policy:
    """Build a policy whose team is the given setup-style specs (cli:x, ollama:y, anthropic:z...)."""
    from looper.setup import parse_agent_spec, unique_names
    from looper.config import merge_config
    base = bundled_default()
    pol0 = build_policy(base)
    choices = unique_names([parse_agent_spec(s, pol0) for s in specs])
    user = {"agents": {c.name: c.entry for c in choices if c.entry}, "team": [c.name for c in choices]}
    if defaults:
        user["defaults"] = defaults
    return build_policy(merge_config(base, user))


def team_available(pol: Policy) -> dict:
    return {n: (f"/usr/bin/{n}" if pol.agents[n].kind == "cli" else pol.agents[n].base_url) for n in pol.team}
