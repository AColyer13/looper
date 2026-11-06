"""`looper setup`: pick models first, then enter each API key, then write the team.

The team goes in looper.json (``agents`` + ``team``); keys go to the credentials
store (``looper.credentials``), never into looper.json.

Agent spec strings (CLI ``--agent`` and the wizard's custom entry)::

    cli:claude                          an installed agent CLI (claude|codex|grok|agy)
    anthropic:claude-sonnet-5-5         hosted API model (anthropic|openai|xai|google|openrouter)
    ollama:qwen3.5:9b                   local model via Ollama
    lmstudio:qwen3.5-4b                 local model via LM Studio
    openai_compatible:my-model@http://localhost:8000/v1    vLLM / llama.cpp / other server
    ...#small                           optional manual tier override suffix
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper.config import ConfigError, user_config_dir
from looper.http_runner import Transport
from looper.policy import Policy

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Choice:
    spec: str
    name: str
    entry: dict[str, Any] | None  # None = existing agent (CLI), no config entry needed
    provider: str
    kind: str
    label: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def default_config_path() -> Path:
    return user_config_dir() / "looper.json"


def parse_agent_spec(spec: str, policy: Policy) -> Choice:
    raw = spec.strip()
    if ":" not in raw:
        if raw in policy.agents and policy.agents[raw].kind == "cli":
            raw = f"cli:{raw}"
        else:
            raise ValueError(f"agent spec {spec!r}: expected provider:model (e.g. ollama:qwen3.5:9b)")
    tier = None
    if "#" in raw:
        raw, tier = raw.rsplit("#", 1)
        if tier not in policy.tiers:
            raise ValueError(f"agent spec {spec!r}: unknown tier '{tier}' (known: {', '.join(policy.tiers)})")
    provider, rest = raw.split(":", 1)
    provider = provider.strip().lower()
    base_url = None
    m = re.search(r"@(https?://\S+)$", rest)
    if m:
        base_url, rest = m.group(1), rest[: m.start()]
    model = rest.strip()
    if not model:
        raise ValueError(f"agent spec {spec!r}: missing model")
    if provider == "cli":
        if model not in policy.agents or policy.agents[model].kind != "cli":
            clis = [n for n, a in policy.agents.items() if a.kind == "cli"]
            raise ValueError(f"unknown CLI agent '{model}' (known: {', '.join(clis)})")
        entry = {"tier": tier} if tier else None
        return Choice(spec, model, entry, "cli", "cli", policy.agents[model].description)
    if provider not in policy.providers:
        raise ValueError(f"unknown provider '{provider}' (known: cli, {', '.join(policy.providers)})")
    prov = policy.providers[provider]
    entry: dict[str, Any] = {"kind": prov.kind, "provider": provider, "model": model}
    if base_url:
        entry["base_url"] = base_url
    if tier:
        entry["tier"] = tier
    name = _SAFE.sub("-", model.split("/")[-1]).strip("-") or provider
    if prov.kind == "local" and provider != "ollama":
        name = f"{provider}-{name}"
    return Choice(spec, name, entry, provider, prov.kind, f"{provider} {model}")


def enrich_local(choice: Choice, policy: Policy, transport: Transport | None = None) -> Choice:
    """Fill params_b / active_params_b / context_window from the local server (best effort)."""
    if not choice.entry or choice.kind != "local":
        return choice
    from looper import local
    prov = policy.providers[choice.provider]
    base = choice.entry.get("base_url") or prov.base_url or ""
    model = choice.entry["model"]
    found: list[local.LocalModel] = []
    if choice.provider == "ollama":
        found = [m for m in local.ollama_models(base, transport=transport) if m.model == model]
    elif choice.provider == "lmstudio":
        found = [m for m in local.lmstudio_models(base, transport=transport) if m.model == model]
    else:
        found = [m for m in local.openai_compatible_models(base, transport=transport) if m.model == model]
    if found:
        f = found[0]
        for k in ("params_b", "active_params_b", "context_window"):
            v = getattr(f, k)
            if v is not None:
                choice.entry[k] = v
        choice.meta["probed"] = f.source
    else:
        choice.meta["probed"] = "unreachable-or-not-found"
    return choice


def unique_names(choices: list[Choice]) -> list[Choice]:
    seen: dict[str, int] = {}
    for c in choices:
        if c.name in seen:
            seen[c.name] += 1
            c.name = f"{c.name}-{c.provider}" if seen[c.name] == 2 else f"{c.name}-{seen[c.name]}"
        else:
            seen[c.name] = 1
    return choices


def needed_keys(choices: list[Choice], policy: Policy) -> dict[str, list[str]]:
    """env var -> providers, for API agents whose key is required."""
    out: dict[str, list[str]] = {}
    for c in choices:
        if c.kind != "api":
            continue
        prov = policy.providers[c.provider]
        if prov.api_key_env and not prov.key_optional:
            out.setdefault(prov.api_key_env, [])
            if c.provider not in out[prov.api_key_env]:
                out[prov.api_key_env].append(c.provider)
    return out


def validate_team_size(n: int, policy: Policy) -> list[str]:
    if n < 1:
        raise ValueError("pick at least 1 agent")
    if n > policy.defaults.max_agents:
        raise ValueError(f"{n} agents exceeds the max of {policy.defaults.max_agents} "
                         "(set defaults.max_agents to raise it; see docs/MODEL_TIERS.md)")
    if n > policy.defaults.recommended_agents:
        return [f"{n} agents is above the sweet spot of {policy.defaults.recommended_agents}; "
                "extra agents mostly add cost and latency"]
    return []


def write_team(path: Path, choices: list[Choice]) -> dict[str, Any]:
    """Merge the chosen agents + team into the config file at ``path``."""
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: invalid JSON: {exc}") from exc
    agents = data.setdefault("agents", {})
    for c in choices:
        if c.entry:
            agents[c.name] = {**agents.get(c.name, {}), **c.entry}
    if not agents:
        data.pop("agents")
    data["team"] = [c.name for c in choices]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def menu(policy: Policy, installed: dict[str, bool], local_models: list[Any]) -> list[tuple[str, str]]:
    """(spec, label) options for the wizard."""
    from looper.tiers import parse_params, tier_for_size
    opts: list[tuple[str, str]] = []
    for n, a in policy.agents.items():
        if a.kind == "cli":
            opts.append((f"cli:{n}", f"{a.description or n} - CLI, frontier tier, can edit files/run commands "
                                     f"[{'installed' if installed.get(n) else 'not found'}]"))
    for pname, p in policy.providers.items():
        if p.kind == "api":
            for mdl in p.models:
                opts.append((f"{pname}:{mdl}", f"{pname} API, frontier tier, text-only (needs ${p.api_key_env})"))
    for lm in local_models:
        total = lm.params_b or parse_params(lm.model).total_b
        size = parse_params(lm.model)
        act = lm.active_params_b or size.active_b
        eff = (total * act) ** 0.5 if (total and act) else total
        tier = tier_for_size(eff, policy).name if eff else policy.defaults.unknown_local_tier
        ctx = f", ctx {lm.context_window}" if lm.context_window else ""
        opts.append((f"{lm.provider}:{lm.model}", f"local {lm.provider}, ~{total or '?'}B{ctx} -> tier {tier}"))
    return opts
