"""Hand AGENTS.md and MCP config to a CLI through its own flags.

Claude Code takes ``--append-system-prompt-file`` and ``--mcp-config`` (a JSON
file; secrets stay in that 0600 file, not on argv). Codex takes ``-c`` config
overrides: AGENTS.md via ``project_doc_fallback_filenames``, MCP commands and
argument lists on argv, and credential values only through the child
environment (``env_vars`` / ``env_http_headers`` name the variables).

Grok and Antigravity have no per-run flag for an arbitrary instructions file
or MCP config, so looper injects ``.looper/AGENTS.md`` into the prompt and
hosts MCP itself.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from looper.config import ConfigError, state_dir
from looper.mcp_config import ServerSpec, contains_secret
from looper.policy import AgentSpec
from looper.workspace import Workspace

_ENV_NAME = re.compile(r"[^A-Za-z0-9]")


@dataclass
class NativePass:
    passes_agents: bool = False
    passes_mcp: bool = False
    extra_argv: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)


def _binary_name(agent: AgentSpec) -> str:
    return Path(agent.binary or agent.name).name


def _private_file(content: str, suffix: str) -> Path:
    """Write ``content`` mode 0600. Prefer ``~/.looper/sessions``."""
    directory = state_dir() / "sessions"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(state_dir(), 0o700)
        except OSError:
            pass
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        fd, name = tempfile.mkstemp(prefix="mcp-", suffix=suffix, dir=directory)
    except OSError:
        fd, name = tempfile.mkstemp(prefix="looper-mcp-", suffix=suffix)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(name, 0o600)
    return Path(name)


def _require_argv_safe(value: str, secrets: list[str], what: str) -> None:
    if contains_secret(value, secrets):
        raise ConfigError(
            f"{what} expands a credential; refusing to place it on argv. "
            "Move the secret to env (stdio) or headers (http) as ${VAR}."
        )


def _claude_mcp_file(servers: list[ServerSpec]) -> Path | None:
    payload: dict[str, dict] = {}
    for server in servers:
        if server.error:
            continue
        if server.transport == "stdio":
            entry: dict = {"command": server.command, "args": list(server.args)}
            if server.env:
                entry["env"] = dict(server.env)
        else:
            entry = {"type": "sse" if server.transport == "sse" else "http", "url": server.url}
            if server.headers:
                entry["headers"] = dict(server.headers)
        payload[server.name] = entry
    if not payload:
        return None
    return _private_file(json.dumps({"mcpServers": payload}), ".json")


def _codex_mcp_args(servers: list[ServerSpec]) -> tuple[list[str], dict[str, str]]:
    argv: list[str] = []
    env: dict[str, str] = {}
    for server in servers:
        if server.error:
            continue
        name = server.name
        if server.transport == "stdio":
            command = server.command or ""
            _require_argv_safe(command, server.secrets, f"codex MCP server {name} command")
            argv += ["-c", f"mcp_servers.{name}.command={json.dumps(command)}"]
            if server.args:
                for arg in server.args:
                    _require_argv_safe(arg, server.secrets, f"codex MCP server {name} args")
                argv += ["-c", f"mcp_servers.{name}.args={json.dumps(list(server.args))}"]
            if server.env:
                argv += ["-c", f"mcp_servers.{name}.env_vars={json.dumps(sorted(server.env))}"]
                env.update(server.env)
        else:
            url = server.url or ""
            _require_argv_safe(url, server.secrets, f"codex MCP server {name} url")
            argv += ["-c", f"mcp_servers.{name}.url={json.dumps(url)}"]
            if server.headers:
                mapping: dict[str, str] = {}
                for header, value in server.headers.items():
                    env_name = _ENV_NAME.sub("_", f"LOOPER_MCP_{name}_{header}").upper()
                    env[env_name] = value
                    mapping[header] = env_name
                argv += ["-c", f"mcp_servers.{name}.env_http_headers={json.dumps(mapping)}"]
    return argv, env


def native_pass(agent: AgentSpec, workspace: Workspace) -> NativePass:
    """Extra argv/env for a CLI agent. Empty when this agent has no native hook."""
    if agent.kind != "cli":
        return NativePass()
    name = _binary_name(agent)
    out = NativePass()
    servers = [s for s in workspace.mcp.servers.values() if not s.error]
    if name == "claude":
        if workspace.agents_path is not None:
            out.passes_agents = True
            out.extra_argv += ["--append-system-prompt-file", str(workspace.agents_path)]
        path = _claude_mcp_file(servers)
        if path is not None:
            out.passes_mcp = True
            out.extra_argv += ["--mcp-config", str(path)]
        return out
    if name == "codex":
        if workspace.agents_path is not None:
            out.passes_agents = True
            # Codex reads AGENTS.md while walking from the git root to cwd, then
            # each name in project_doc_fallback_filenames. The looper file lives
            # at .looper/AGENTS.md, so that relative name is the native hook.
            out.extra_argv += [
                "-c",
                'project_doc_fallback_filenames=["AGENTS.md", ".looper/AGENTS.md"]',
            ]
        mcp_argv, mcp_env = _codex_mcp_args(servers)
        if mcp_argv:
            out.passes_mcp = True
            out.extra_argv += mcp_argv
            out.extra_env.update(mcp_env)
        return out
    return out
