"""MCP server config and the tool approval policy.

Config is merged from (later sources override a server entry and the mode;
allow/deny lists accumulate):

1. ``mcp`` in the active looper.json (wherever config discovery found it)
2. ``mcp`` in ``~/.looper/looper.json`` when that file is not already the active config
3. ``<project>/.mcp.json`` (Claude Code ``mcpServers`` shape)
4. ``<project>/.looper/mcp.json``

Env values may use ``${VAR}``. The variable is read from the process environment,
the OS keyring, or ``~/.config/looper/credentials.env``. Expanded values stay in
memory for the child process; they are never written back to a project file.

Approval decision, first match wins:

    deny  >  require_approval  >  allow / auto_approve  >  annotation default

The annotation default depends on the mode (``auto_read_only`` unless a role,
server, or config sets another):

* ``auto_read_only`` — ``readOnlyHint: true`` and not ``destructiveHint`` runs
  inside looper. Anything else (writes, destructive, unannotated) is pending.
* ``approve_all`` — annotation default is approval.
* ``deny_all`` — annotation default is deny.

An explicit allow/auto_approve still runs under ``deny_all``. An explicit deny
still refuses under ``approve_all``. ``require_approval`` beats allow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from looper.config import ConfigError, state_dir
from looper.credentials import get_key

MODES = ("auto_read_only", "approve_all", "deny_all")
_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ToolLists:
    deny: list[str] = field(default_factory=list)
    require_approval: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    auto_approve: list[str] = field(default_factory=list)

    def extend(self, other: "ToolLists") -> None:
        self.deny.extend(other.deny)
        self.require_approval.extend(other.require_approval)
        self.allow.extend(other.allow)
        self.auto_approve.extend(other.auto_approve)


@dataclass
class ServerSpec:
    name: str
    transport: str  # stdio | http | sse
    source: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    lists: ToolLists = field(default_factory=ToolLists)
    mode: str | None = None
    error: str | None = None
    # Expanded credential values. Never serialize, never put on argv.
    secrets: list[str] = field(default_factory=list, repr=False)


@dataclass
class McpSettings:
    mode: str = "auto_read_only"
    lists: ToolLists = field(default_factory=ToolLists)
    servers: dict[str, ServerSpec] = field(default_factory=dict)

    @property
    def deny(self) -> list[str]:
        return self.lists.deny

    @property
    def require_approval(self) -> list[str]:
        return self.lists.require_approval

    @property
    def allow(self) -> list[str]:
        return self.lists.allow

    @property
    def auto_approve(self) -> list[str]:
        return self.lists.auto_approve


@dataclass
class RolePolicy:
    """Per-role overrides from ``roles/<role>.md`` frontmatter."""

    mode: str | None = None
    lists: ToolLists = field(default_factory=ToolLists)

    @property
    def deny(self) -> list[str]:
        return self.lists.deny

    @property
    def require_approval(self) -> list[str]:
        return self.lists.require_approval

    @property
    def allow(self) -> list[str]:
        return self.lists.allow

    @property
    def auto_approve(self) -> list[str]:
        return self.lists.auto_approve


def empty_settings() -> McpSettings:
    return McpSettings()


def expose_tool(server: str, tool: str) -> str:
    """Name offered to the model. ``__`` separates server from tool."""
    return f"mcp__{server}__{tool}"


def parse_exposed_tool(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def _as_str_list(value: Any, where: str) -> list[str]:
    if value is None or value is False:
        return []
    if value is True:
        return ["*"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(f"{where}[{i}] must be a string")
            out.append(item)
        return out
    raise ConfigError(f"{where} must be a string or list of strings")


def _lists_from(data: dict[str, Any], where: str) -> ToolLists:
    return ToolLists(
        deny=_as_str_list(data.get("deny"), f"{where}.deny"),
        require_approval=_as_str_list(data.get("require_approval"), f"{where}.require_approval"),
        allow=_as_str_list(data.get("allow"), f"{where}.allow"),
        auto_approve=_as_str_list(data.get("auto_approve"), f"{where}.auto_approve"),
    )


def _mode(value: Any, where: str) -> str | None:
    if value is None:
        return None
    mode = str(value)
    if mode not in MODES:
        raise ConfigError(f"{where} must be one of {', '.join(MODES)}")
    return mode


def _expand(value: str, secrets: list[str], where: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        got = get_key(name)
        if not got:
            raise ConfigError(
                f"{where}: ${{{name}}} is not set "
                "(environment, keyring, or ~/.config/looper/credentials.env)"
            )
        secrets.append(got)
        return got

    return _PLACEHOLDER.sub(repl, value)


def _expand_map(raw: Any, secrets: list[str], where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object of strings")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            raise ConfigError(f"{where}.{k} must be a string")
        out[str(k)] = _expand(v, secrets, f"{where}.{k}")
    return out


def parse_server(name: str, raw: Any, source: str) -> ServerSpec:
    where = f"{source} server {name!r}"
    if not _SERVER_NAME.match(name):
        raise ConfigError(f"{where}: name must match {_SERVER_NAME.pattern}")
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object")
    secrets: list[str] = []
    transport_raw = str(raw.get("transport") or raw.get("type") or "").lower().replace("_", "-")
    url = raw.get("url") or raw.get("serverUrl") or raw.get("httpUrl")
    command = raw.get("command")
    if transport_raw in ("sse", "http", "streamable-http"):
        transport = "sse" if transport_raw == "sse" else "http"
    elif url and not command:
        transport = "http"
    elif command:
        transport = "stdio"
    else:
        raise ConfigError(f"{where}: set command (stdio) or url (http)")
    if transport == "stdio":
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"{where}: command must be a string")
        args_raw = raw.get("args") or []
        if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
            raise ConfigError(f"{where}: args must be a list of strings")
        try:
            args = [_expand(a, secrets, f"{where}.args") for a in args_raw]
            command = _expand(command, secrets, f"{where}.command")
            env = _expand_map(raw.get("env"), secrets, f"{where}.env")
        except ConfigError as exc:
            spec = ServerSpec(name=name, transport=transport, source=source, error=str(exc), secrets=secrets)
            return spec
        return ServerSpec(
            name=name, transport="stdio", source=source, command=command, args=args, env=env,
            include=_as_str_list(raw.get("include"), f"{where}.include"),
            exclude=_as_str_list(raw.get("exclude"), f"{where}.exclude"),
            lists=_lists_from(raw, where),
            mode=_mode(raw.get("mode"), f"{where}.mode"),
            secrets=secrets,
        )
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{where}: url is required for {transport}")
    try:
        url = _expand(url, secrets, f"{where}.url")
        headers = _expand_map(raw.get("headers"), secrets, f"{where}.headers")
    except ConfigError as exc:
        return ServerSpec(name=name, transport=transport, source=source, error=str(exc), secrets=secrets)
    return ServerSpec(
        name=name, transport=transport, source=source, url=url, headers=headers,
        include=_as_str_list(raw.get("include"), f"{where}.include"),
        exclude=_as_str_list(raw.get("exclude"), f"{where}.exclude"),
        lists=_lists_from(raw, where),
        mode=_mode(raw.get("mode"), f"{where}.mode"),
        secrets=secrets,
    )


def _servers_blob(data: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key in ("servers", "mcpServers"):
        block = data.get(key)
        if isinstance(block, dict):
            found.update(block)
    return found


def parse_mcp_mapping(data: dict[str, Any], source: str) -> tuple[str | None, ToolLists, dict[str, ServerSpec]]:
    """Parse one file. A nested ``mcp`` object is the looper.json shape."""
    if "mcp" in data and isinstance(data["mcp"], dict) and not _servers_blob(data):
        data = data["mcp"]
        source = f"{source} mcp"
    mode = _mode(data.get("mode"), f"{source} mode")
    lists = _lists_from(data, source)
    servers: dict[str, ServerSpec] = {}
    for name, spec in _servers_blob(data).items():
        servers[str(name)] = parse_server(str(name), spec, source)
    return mode, lists, servers


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")
    return data


def load_mcp_settings(
    policy_raw: dict[str, Any] | None = None,
    policy_path: Path | None = None,
    project_root: Path | None = None,
) -> McpSettings:
    """Merge every MCP source. Missing files are skipped."""
    settings = McpSettings()
    blobs: list[tuple[str, dict[str, Any]]] = []
    if isinstance(policy_raw, dict) and isinstance(policy_raw.get("mcp"), dict):
        blobs.append((f"{policy_path or 'config'} mcp", policy_raw["mcp"]))
    state_file = state_dir() / "looper.json"
    same = policy_path is not None and state_file.resolve() == Path(policy_path).resolve()
    if not same and state_file.is_file():
        raw = _read_json(state_file)
        if isinstance(raw.get("mcp"), dict):
            blobs.append((f"{state_file} mcp", raw["mcp"]))
    if project_root is not None:
        claude = project_root / ".mcp.json"
        project = project_root / ".looper" / "mcp.json"
        if claude.is_file():
            blobs.append((str(claude), _read_json(claude)))
        if project.is_file():
            blobs.append((str(project), _read_json(project)))
    for source, data in blobs:
        mode, lists, servers = parse_mcp_mapping(data, source)
        if mode:
            settings.mode = mode
        settings.lists.extend(lists)
        settings.servers.update(servers)
    return settings


def _match(patterns: list[str], server: str, tool: str) -> bool:
    if not patterns:
        return False
    candidates = (tool, f"{server}:{tool}", f"{server}/{tool}", expose_tool(server, tool))
    for pat in patterns:
        for cand in candidates:
            if fnmatchcase(cand, pat):
                return True
    return False


def tool_visible(server: ServerSpec, tool: str) -> bool:
    if server.include and not _match(server.include, server.name, tool):
        return False
    if server.exclude and _match(server.exclude, server.name, tool):
        return False
    return True


def decide(
    settings: McpSettings,
    server: ServerSpec,
    tool: str,
    annotations: dict[str, Any] | None,
    role: RolePolicy | None = None,
) -> tuple[str, str]:
    """Return ``(action, reason)`` where action is ``deny``, ``approval``, or ``auto``."""
    role = role or RolePolicy()
    scopes = (role.lists, server.lists, settings.lists)
    if any(_match(s.deny, server.name, tool) for s in scopes):
        return "deny", "deny"
    if any(_match(s.require_approval, server.name, tool) for s in scopes):
        return "approval", "require_approval"
    if any(_match(s.allow, server.name, tool) or _match(s.auto_approve, server.name, tool) for s in scopes):
        return "auto", "auto_approve"
    mode = role.mode or server.mode or settings.mode or "auto_read_only"
    if mode == "deny_all":
        return "deny", "deny_all"
    if mode == "approve_all":
        return "approval", "approve_all"
    ann = annotations or {}
    read_only = ann.get("readOnlyHint") is True
    destructive = ann.get("destructiveHint") is True
    if read_only and not destructive:
        return "auto", "read_only"
    if destructive:
        return "approval", "destructive"
    return "approval", "not_read_only"


def contains_secret(text: str, secrets: list[str]) -> bool:
    return any(s and s in text for s in secrets)


def scrub(text: str, secrets: list[str]) -> str:
    """Replace expanded credential values. Last-4 masking matches ``credentials.mask``."""
    from looper.credentials import mask

    out = text
    # Longest first so a short secret isn't a prefix of a longer one left behind.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(secret, mask(secret))
    return out
