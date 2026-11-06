"""Stable, versioned machine interface shared by the CLI and ``looper serve``.

Frontends (e.g. the-office) should prefer these helpers / the matching CLI flags
and HTTP endpoints over scraping human output. See ``docs/FRONTEND_API.md``.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from looper import __version__
from looper.assign import agent_endpoint, build_members, default_available
from looper.config import ConfigError, merge_config
from looper.credentials import get_key, key_source, mask, set_key, status as key_status
from looper.detect import detect_agents
from looper.events import API_VERSION
from looper.policy import AgentSpec, Policy, build_policy
from looper.runners import (CallResult, DispatchRunner, Invocation, PromptTooLongError,
                           Runner, describe_call, prepare_invocation)
from looper.tiers import resolve_tier

CAPABILITIES = [
    "version",
    "doctor",
    "plan",
    "run_events",
    "ask",
    "ask_stdin",
    "ask_tools",
    "tool_calls",
    "tool_results",
    "serve",
    "team_override",
    "assign_override",
    "role_labels",
    "meta",
    "keys",
    "models_discover",
]


def version_info() -> dict[str, Any]:
    return {
        "name": "looper",
        "version": __version__,
        "api_version": API_VERSION,
        "capabilities": list(CAPABILITIES),
    }


# --------------------------------------------------------------------------- team override


def apply_team_override(policy: Policy, team_data: dict[str, Any] | list[Any] | None) -> Policy:
    """Return a new Policy whose team comes from ``team_data``.

    Accepted shapes (same as user config)::

        {"agents": {"name": {...}, ...}, "team": ["name", ...]}
        {"team": [{"name": "...", "kind": "api", ...}, ...]}   # inline agent list
        [{"name": "...", ...}, ...]                            # shorthand for the above

    Existing agents are shallow-merged; unknown names are added. Keys are never
    taken from this payload — only from the credentials store / process env.
    """
    if team_data is None:
        return policy
    if isinstance(team_data, list):
        team_data = {"team": team_data}
    if not isinstance(team_data, dict):
        raise ConfigError("team override must be an object or list of agents")

    user: dict[str, Any] = {}
    agents_in = team_data.get("agents")
    team_in = team_data.get("team")

    if isinstance(agents_in, dict):
        user["agents"] = deepcopy(agents_in)

    names: list[str] = []
    if isinstance(team_in, list):
        inline: dict[str, Any] = {}
        for item in team_in:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if not name:
                    raise ConfigError("team[].name is required for inline agents")
                name = str(name)
                names.append(name)
                entry = {k: v for k, v in item.items() if k != "name"}
                if entry:
                    inline[name] = entry
            else:
                raise ConfigError(f"team[] entry must be a string or object, got {type(item).__name__}")
        if inline:
            user.setdefault("agents", {}).update(inline)
        user["team"] = names
    elif team_in is not None:
        raise ConfigError("team must be a list of agent names or agent objects")
    elif "agents" in user:
        user["team"] = list(user["agents"])

    if not user:
        return policy

    # Drop keys that must never arrive via team payload
    for a in (user.get("agents") or {}).values():
        if isinstance(a, dict):
            a.pop("api_key", None)
            a.pop("api_key_value", None)
            a.pop("key", None)

    merged = merge_config(policy.raw, user)
    return build_policy(merged, source=f"{policy.source}+team-override", path=policy.path)


def load_team_file(path: Path | str) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"team file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"team file {p}: invalid JSON ({exc})") from exc
    if not isinstance(data, (dict, list)):
        raise ConfigError(f"team file {p}: expected object or list")
    return data  # type: ignore[return-value]


def parse_json_arg(raw: str | None, *, what: str) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{what}: invalid JSON ({exc})") from exc


def parse_meta(pairs: list[str] | None = None, meta_json: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if meta_json:
        data = parse_json_arg(meta_json, what="--meta-json")
        if not isinstance(data, dict):
            raise ConfigError("--meta-json must be a JSON object")
        out.update(data)
    for p in pairs or []:
        if "=" not in p:
            raise ConfigError(f"--meta expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        if not k:
            raise ConfigError(f"--meta empty key in {p!r}")
        # try JSON value, else string
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def parse_role_labels(raw: str | None) -> dict[str, str]:
    data = parse_json_arg(raw, what="--role-labels-json")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("--role-labels-json must be a JSON object of role->label")
    return {str(k): str(v) for k, v in data.items()}


def parse_assign(raw: str | None) -> dict[str, str]:
    """role (concrete or abstract) -> agent name."""
    data = parse_json_arg(raw, what="--assign-json")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("--assign-json must be a JSON object of role->agent")
    return {str(k): str(v) for k, v in data.items()}


def validate_assign(policy: Policy, assign: dict[str, str]) -> list[str]:
    """Return warnings; raise ConfigError on unknown agents."""
    warnings: list[str] = []
    for role, agent in assign.items():
        if agent not in policy.agents:
            raise ConfigError(f"--assign-json: unknown agent '{agent}' for role '{role}' "
                              f"(known: {', '.join(policy.agents)})")
        # soft: if role maps to an abstract and agent tier disallows it
        from looper.assign import allows, build_members
        from looper.policy import RoleSpec
        avail, _ = default_available(policy)
        if agent not in avail:
            # still allow pinning; doctor/plan will surface unreadiness
            warnings.append(f"assigned agent '{agent}' for role '{role}' is not currently ready")
            continue
        members = {m.name: m for m in build_members(policy, {agent: avail[agent]})}
        m = members[agent]
        # try concrete role first, then as abstract
        role_spec = policy.roles.get(role) or RoleSpec(name=role, abstract=role)
        if not allows(m, role_spec, policy):
            warnings.append(
                f"assigned agent '{agent}' (tier {m.tier.name}) is under-tier for role '{role}' "
                f"(abstract '{role_spec.abstract}')")
    return warnings


# --------------------------------------------------------------------------- doctor


def doctor(policy: Policy, *, probe: bool = True) -> dict[str, Any]:
    """Health check for frontends. Never includes key values."""
    statuses = detect_agents(policy, probe=probe)
    avail, missing = default_available(policy)
    members = []
    problems: list[str] = []

    names = policy.team if policy.team is not None else list(policy.agents)
    if policy.team is None:
        problems.append("no team set: run `looper setup` (or pass --team-json) so frontends know which agents to use")
    elif not policy.team:
        problems.append("team is empty")

    for n in names:
        a = policy.agents[n]
        st = statuses.get(n)
        ti = resolve_tier(a, policy)
        key_env = a.api_key_env
        key_ok = True
        key_present = False
        if a.kind == "api" and key_env and not a.key_optional:
            key_present = bool(get_key(key_env))
            key_ok = key_present
            if not key_present:
                problems.append(f"agent '{n}': missing ${key_env}")
        elif a.kind == "api" and key_env:
            key_present = bool(get_key(key_env))
        elif a.kind == "cli" and not (st and st.installed):
            problems.append(f"agent '{n}': CLI `{a.binary}` not on PATH")
        members.append({
            "name": n,
            "kind": a.kind,
            "provider": a.provider,
            "model": a.model or a.binary,
            "tier": ti.tier,
            "size": ti.size,
            "installed": bool(st.installed) if st else False,
            "ready": n in avail,
            "reason": missing.get(n) or (st.reason if st else ""),
            "key_env": key_env,
            "key_present": key_present if key_env else None,
            "base_url": a.base_url,
        })

    clis = {n: s.to_dict() for n, s in statuses.items() if s.kind == "cli"}

    local_servers: list[dict[str, Any]] = []
    try:
        from looper.local import discover_all
        found = discover_all()
        by_url: dict[str, list[str]] = {}
        for m in found:
            by_url.setdefault(m.base_url, []).append(m.model)
        for url, models in by_url.items():
            local_servers.append({"base_url": url, "reachable": True, "models": models[:20],
                                  "model_count": len(models)})
        # also note configured local bases that didn't answer
        seen = {s["base_url"].rstrip("/") for s in local_servers}
        for n in names:
            a = policy.agents[n]
            if a.kind == "local" and a.base_url and a.base_url.rstrip("/") not in seen:
                local_servers.append({"base_url": a.base_url, "reachable": False, "models": [],
                                      "model_count": 0, "agent": n})
                problems.append(f"local server unreachable for agent '{n}' at {a.base_url}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"local discovery failed: {exc}")

    ready = [m for m in members if m["ready"]]
    usable = bool(ready)
    if not usable:
        problems.append("no ready agents: install a CLI, set an API key, or start a local server")

    return {
        "ok": usable and not any("missing" in p or "not on PATH" in p or "unreachable" in p for p in problems
                                 if p.startswith("agent ") or p.startswith("local ") or p.startswith("no ready")),
        "usable": usable,
        "api_version": API_VERSION,
        "looper_version": __version__,
        "config": {
            "source": policy.source,
            "path": str(policy.path) if policy.path else None,
        },
        "team": members,
        "team_size": len(members),
        "max_agents": policy.defaults.max_agents,
        "recommended_agents": policy.defaults.recommended_agents,
        "clis": clis,
        "local_servers": local_servers,
        "problems": problems,
    }


# --------------------------------------------------------------------------- plan enrichment


def enrich_plan(
    plan: dict[str, Any],
    *,
    role_labels: dict[str, str] | None = None,
    meta: dict[str, Any] | None = None,
    assign: dict[str, str] | None = None,
    assign_warnings: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(plan)
    out["api_version"] = API_VERSION
    out["looper_version"] = __version__
    if meta:
        out["meta"] = meta
    if role_labels:
        out["role_labels"] = role_labels
        for s in out.get("stages") or []:
            role = s.get("role") or ""
            abstract = s.get("abstract_role") or ""
            label = role_labels.get(role) or role_labels.get(abstract)
            if label:
                s["label"] = label
    if assign:
        out["assign"] = assign
    if assign_warnings:
        out.setdefault("warnings", [])
        out["warnings"] = list(out["warnings"]) + list(assign_warnings)
    return out


# --------------------------------------------------------------------------- ask (single turn)


def parse_ask_request(raw: dict | None = None, **fields: Any) -> dict[str, Any]:
    """Normalize an ask request from HTTP body / ``--request-json`` / kwargs.

    Accepted keys: prompt, system, tools, messages, tool_results, max_tool_calls,
    agent, cwd, timeout, max_tokens, temperature, protocol (force).
    """
    data = dict(raw or {})
    for k, v in fields.items():
        if v is not None and k not in data:
            data[k] = v
    return data


def ask(
    policy: Policy,
    agent_name: str,
    prompt: str | None = None,
    *,
    request: dict[str, Any] | None = None,
    runner: Runner | None = None,
    available: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    system: str | None = None,
    tools: list[Any] | None = None,
    messages: list[Any] | None = None,
    tool_results: list[Any] | None = None,
    max_tool_calls: int | None = None,
    protocol: str | None = None,
) -> dict[str, Any]:
    """Send one prompt (optionally with tools) to one team agent.

    Returns ``{ok, text, tool_calls, finish_reason, protocol, messages, agent, ...}``.
    Plain text with ``tool_calls: []`` is the valid fallback when no tools were
    offered or the model chose not to call any. looper never executes tools.
    """
    from looper import tools as toolmod
    from looper.assign import max_tokens_for
    from looper.tiers import resolve_tier

    req = parse_ask_request(
        request,
        prompt=prompt,
        system=system,
        tools=tools,
        messages=messages,
        tool_results=tool_results,
        max_tool_calls=max_tool_calls,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        max_tokens=max_tokens,
        temperature=temperature,
        protocol=protocol,
    )
    if agent_name not in policy.agents:
        raise ConfigError(f"unknown agent '{agent_name}' (known: {', '.join(policy.agents)})")
    agent = policy.agents[agent_name]

    try:
        tool_specs = toolmod.parse_tools(req.get("tools"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    try:
        seeded = toolmod.seed_messages(
            prompt=(req.get("prompt") or None),
            system=req.get("system"),
            messages=req.get("messages"),
            tool_results=req.get("tool_results"),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    # Resolve readiness
    if available is None:
        available, missing = default_available(policy)
        if agent_name not in available:
            ep, why = agent_endpoint(agent)
            if ep is None:
                raise ConfigError(f"agent '{agent_name}' is not ready: {why}")
            available = {agent_name: ep}
    elif agent_name not in available:
        ep, why = agent_endpoint(agent)
        if ep is None:
            raise ConfigError(f"agent '{agent_name}' is not ready: {why}")
        available = {**available, agent_name: ep}

    members = {m.name: m for m in build_members(policy, available)}
    m = members.get(agent_name)
    ti = resolve_tier(agent, policy)
    tier_spec = policy.tiers.get(ti.tier)
    structured = tier_spec.structured_output if tier_spec else "good"

    proto, proto_warnings = toolmod.choose_protocol(
        tools=tool_specs,
        agent_kind=agent.kind,
        api_style=agent.api_style,
        tier_name=ti.tier,
        structured_output=structured,
        force=req.get("protocol"),
    )
    mt_limit = toolmod.max_tool_calls_for(proto, ti.tier, req.get("max_tool_calls"))

    # Build the prompt string for CLI / argv (HTTP uses messages)
    user_bits = [x.get("content", "") for x in seeded if x.get("role") == "user"]
    prompt_text = user_bits[-1] if user_bits else (req.get("prompt") or "")
    system_text = next((x.get("content", "") for x in seeded if x.get("role") == "system"), None)

    # For json_block / CLI: inject tool instructions into system (or prepend to prompt)
    effective_system = system_text
    effective_messages = list(seeded)
    if proto == "json_block" and tool_specs:
        instr = toolmod.json_block_instructions(tool_specs, max_calls=mt_limit)
        if effective_system:
            effective_system = effective_system.rstrip() + "\n\n" + instr
        else:
            effective_system = instr
        # rebuild messages with updated system
        effective_messages = toolmod.seed_messages(
            prompt=None,
            system=effective_system,
            messages=[x for x in seeded if x.get("role") != "system"],
            tool_results=None,  # already applied in seeded
        )
        # seeded already has tool results; the filter above dropped system only.
        # Re-apply by taking non-system from seeded and new system:
        effective_messages = [{"role": "system", "content": effective_system}] + [
            x for x in seeded if x.get("role") != "system"
        ]
    elif proto == "none" and tool_specs:
        # tiny tier refusal — still call the model for text, without tools
        pass

    # CLI delivery uses a flattened prompt (system + conversation summary)
    if agent.kind == "cli":
        parts: list[str] = []
        if effective_system:
            parts.append(effective_system)
        for msg in effective_messages:
            if msg.get("role") == "system":
                continue
            role = msg.get("role", "user")
            if role == "tool":
                parts.append(
                    f"[tool_result id={msg.get('tool_call_id')} tool={msg.get('tool','')} ok={msg.get('ok')}]\n"
                    f"{msg.get('content','')}"
                )
            elif role == "assistant":
                parts.append(f"[assistant]\n{msg.get('content','')}")
                for tc in msg.get("tool_calls") or []:
                    parts.append(f"[prior_tool_call] {tc.get('tool')} {tc.get('args')}")
            else:
                parts.append(msg.get("content") or "")
        cli_prompt = "\n\n".join(p for p in parts if p)
    else:
        cli_prompt = prompt_text

    endpoint = available[agent_name]
    to = float(req.get("timeout") or timeout or agent.timeout_seconds or policy.defaults.timeout_seconds)
    mt = req.get("max_tokens") if req.get("max_tokens") is not None else (
        max_tokens if max_tokens is not None else (max_tokens_for(m) if m else None)
    )
    temp = req.get("temperature") if req.get("temperature") is not None else temperature
    work_cwd = req.get("cwd") or (str(cwd) if cwd else None)

    extra_argv: list[str] = []
    if agent.kind == "cli" and tool_specs and proto != "none":
        # Prefer per-agent config override if present on the raw agent entry
        agent_extra = None
        raw_agents = (policy.raw or {}).get("agents") or {}
        if isinstance(raw_agents.get(agent.name), dict):
            agent_extra = raw_agents[agent.name].get("caller_tools_extra_args")
        extra_argv = toolmod.cli_extra_args_for_caller_tools(agent.name, agent_extra)

    agent_info = {
        "name": agent.name,
        "kind": agent.kind,
        "provider": agent.provider,
        "model": agent.model or agent.binary,
        "tier": m.tier.name if m else ti.tier,
    }

    def _fail(err: str, **extra: Any) -> dict[str, Any]:
        out = {
            "ok": False,
            "text": "",
            "tool_calls": [],
            "finish_reason": "error",
            "protocol": proto,
            "messages": effective_messages,
            "agent": agent_info,
            "seconds": 0.0,
            "error": err,
            "usage": None,
            "warnings": list(proto_warnings) + list(extra.pop("warnings", [])),
        }
        out.update(extra)
        return out

    try:
        inv = prepare_invocation(
            agent, endpoint, cli_prompt, to, work_cwd,
            max_tokens=mt, temperature=temp,
            num_ctx=m.info.context_tokens if m and agent.kind == "local" else None,
            disable_thinking=bool(m and m.tier.disable_thinking),
            system=effective_system,
            messages=effective_messages if agent.kind != "cli" else [],
            tools=tool_specs if proto != "none" else [],
            protocol=proto if proto != "none" else "none",
            max_tool_calls=mt_limit,
            extra_argv=extra_argv,
        )
    except PromptTooLongError as exc:
        return _fail(str(exc))

    run = runner or DispatchRunner()
    res: CallResult = run(inv)

    text = res.output if res.ok else ""
    tool_calls = list(res.tool_calls or [])
    finish = res.finish_reason or ("stop" if res.ok else "error")
    warnings = list(proto_warnings) + list(res.warnings or [])

    # CLI / FakeRunner path: parse json_block from text when the runner didn't
    if res.ok and proto == "json_block" and tool_specs and not tool_calls:
        text, tool_calls, w = toolmod.parse_json_block(text, tool_specs, max_calls=mt_limit)
        warnings.extend(w)
        if tool_calls:
            finish = "tool_calls"

    if proto == "none" and tool_specs:
        # Ensure we never return tool_calls for tiny-tier refusal
        tool_calls = []
        finish = "stop" if res.ok else "error"

    messages_out = toolmod.append_assistant_message(effective_messages, text, tool_calls) if res.ok else effective_messages

    out: dict[str, Any] = {
        "ok": res.ok,
        "text": text if res.ok else "",
        "tool_calls": tool_calls if res.ok else [],
        "finish_reason": finish if res.ok else "error",
        "protocol": proto,
        "messages": messages_out,
        "agent": agent_info,
        "seconds": round(res.seconds, 3),
        "error": res.error,
        "usage": res.usage,
        "warnings": warnings,
    }
    if not res.ok:
        out["stderr"] = res.stderr
    return out


# --------------------------------------------------------------------------- keys (for serve)


def put_key(policy: Policy, provider_or_env: str, value: str, *, use_keyring: bool = False) -> dict[str, Any]:
    env = provider_or_env
    if provider_or_env in policy.providers:
        env = policy.providers[provider_or_env].api_key_env or ""
        if not env:
            raise ConfigError(f"provider '{provider_or_env}' does not use an API key")
    where = set_key(env, value, use_keyring=use_keyring)
    return {"env": env, "source": "keyring" if where == "keyring" else "file",
            "masked": mask(value.strip()), "providers": [
                p for p, v in policy.providers.items() if v.api_key_env == env]}


def list_keys(policy: Policy) -> list[dict[str, Any]]:
    envs: dict[str, list[str]] = {}
    for p, v in policy.providers.items():
        if v.api_key_env:
            envs.setdefault(v.api_key_env, []).append(p)
    return [asdict(r) if hasattr(r, "__dataclass_fields__") else r.__dict__ for r in key_status(envs)]


def discover_models(ollama_url: str = "http://localhost:11434",
                    lmstudio_url: str = "http://localhost:1234") -> list[dict[str, Any]]:
    from looper.local import discover_all
    from looper.tiers import parse_params, tier_for_size
    from looper.policy import load_policy
    # tiers from bundled defaults is fine for discovery ranking
    pol = load_policy()
    out = []
    for lm in discover_all(ollama_url=ollama_url, lmstudio_url=lmstudio_url):
        s = parse_params(lm.model)
        tot, act = lm.params_b or s.total_b, lm.active_params_b or s.active_b
        eff = (tot * act) ** 0.5 if tot and act else tot
        out.append({**lm.to_dict(),
                    "tier": tier_for_size(eff, pol).name if eff else pol.defaults.unknown_local_tier})
    return out
