"""Detect agents: CLIs via shutil.which + optional --version probe; api/local via key/config."""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from looper.policy import AgentSpec, Policy


@dataclass
class AgentStatus:
    name: str
    binary: str
    installed: bool
    path: str | None
    version: str | None = None
    description: str = ""
    kind: str = "cli"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_binary(agent: AgentSpec) -> str:
    """Binary for an agent; ``$LOOPER_<NAME>_BIN`` beats config ``binary``."""
    return os.environ.get(f"LOOPER_{agent.name.upper()}_BIN") or agent.binary


def probe_version(path: str, args: list[str], timeout: float) -> str | None:
    try:
        cp = subprocess.run(
            [path, *args], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (cp.stdout or cp.stderr or "").strip()
    return text.splitlines()[0][:80] if text else None


def detect_agent(agent: AgentSpec, *, probe: bool = True, timeout: float = 3) -> AgentStatus:
    if agent.kind != "cli":
        from looper.assign import agent_endpoint
        ep, why = agent_endpoint(agent)
        return AgentStatus(agent.name, agent.model or "", ep is not None, agent.base_url, agent.model,
                           agent.description or f"{agent.provider} {agent.model}", agent.kind, why)
    binary = resolve_binary(agent)
    path = shutil.which(os.path.expanduser(binary))
    version = probe_version(path, agent.version_args, timeout) if (path and probe and agent.version_args) else None
    return AgentStatus(agent.name, binary, path is not None, path, version, agent.description, "cli",
                       "installed" if path else "not on PATH")


def detect_agents(policy: Policy, *, probe: bool = True, names: list[str] | None = None) -> dict[str, AgentStatus]:
    agents = [policy.agents[n] for n in names] if names else list(policy.agents.values())
    timeout = policy.defaults.version_probe_timeout
    with ThreadPoolExecutor(max_workers=max(1, len(agents))) as pool:
        results = list(pool.map(lambda a: detect_agent(a, probe=probe, timeout=timeout), agents))
    return {s.name: s for s in results}
