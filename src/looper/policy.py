"""Policy / strategy layer.

Turns the raw JSON config into validated, typed objects: agents, role
strategies (role -> agent preference list), loops (stages + iteration + caps),
and router rules. Everything downstream (router, executor, CLI) works off a
``Policy``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from looper.config import ConfigError, load_raw_config

STAGE_TYPES = ("single", "parallel", "map")
AGENT_KINDS = ("cli", "api", "local")
API_STYLES = ("openai", "openai_responses", "anthropic", "ollama")


@dataclass
class AgentSpec:
    """One team member. ``kind=cli`` runs a subprocess; ``api``/``local`` call an HTTP endpoint."""

    name: str
    binary: str = ""
    command: list[str] = field(default_factory=list)
    description: str = ""
    version_args: list[str] = field(default_factory=lambda: ["--version"])
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
    prompt_via: str = "arg"  # "arg" | "stdin" | "auto" (auto = arg until long, then stdin)
    # v0.3 registry fields
    kind: str = "cli"
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    key_optional: bool = False
    api_style: str | None = None
    tier: str | None = None  # manual override; else auto
    params_b: float | None = None  # total params (billions)
    active_params_b: float | None = None  # MoE active params
    context_window: int | None = None  # model max tokens
    num_ctx: int | None = None  # runtime context to request (local)
    max_output_tokens: int | None = None
    roles: list[str] = field(default_factory=list)  # allowed roles; empty = any the tier allows
    tools: bool | None = None  # can read files / run commands (default: cli=True, else False)

    @property
    def has_tools(self) -> bool:
        return self.kind == "cli" if self.tools is None else bool(self.tools)


@dataclass
class TierSpec:
    name: str
    rank: int
    max_params_b: float | None
    roles: list[str]
    num_ctx: int | None = None
    max_output_tokens: int = 4096
    max_context_chars: int = 20000
    structured_output: str = "good"
    iteration_multiplier: float = 1.0
    can_orchestrate: bool = True
    max_map_items: int = 6
    best_of_n: bool = True
    simplify_prompts: bool = False
    disable_thinking: bool = False
    examples: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class RoleSpec:
    name: str
    abstract: str
    lead: bool = False
    tools: bool = False
    diverse_from: str | None = None


@dataclass
class ProviderSpec:
    name: str
    kind: str
    api_style: str | None
    base_url: str | None
    api_key_env: str | None
    key_optional: bool = False
    context_window: int | None = None
    models: list[str] = field(default_factory=list)
    vendor: str | None = None  # CLI agent from the same vendor (inherits its strategy preference)


@dataclass
class Caps:
    max_total_calls: int
    max_wall_seconds: float


@dataclass
class StageSpec:
    name: str
    role: str
    prompt: str
    agents: list[str] = field(default_factory=list)  # explicit override; else strategies[role]
    type: str = "single"
    n: int = 1  # parallel candidates
    source: str | None = None  # map: stage whose output is split into items
    max_items: int = 6
    max_workers: int = 4
    timeout_seconds: float | None = None
    artifact: bool = False
    optional: bool = False  # may be skipped when the team can't staff it / lite mode
    mergeable: bool = False  # may be folded into the previous stage


@dataclass
class IterateSpec:
    body: list[str]
    max_iterations: int
    exit_signals: list[str]
    check_stages: list[str]


@dataclass
class RouteRule:
    pattern: str
    weight: float
    label: str
    regex: re.Pattern[str]

    def matches(self, text: str) -> bool:
        return bool(self.regex.search(text))


@dataclass
class LoopSpec:
    name: str
    description: str
    stages: list[StageSpec]
    caps: Caps
    iterate: IterateSpec | None = None
    priority: int = 0
    route: list[RouteRule] = field(default_factory=list)

    def stage(self, name: str) -> StageSpec:
        for s in self.stages:
            if s.name == name:
                return s
        raise KeyError(name)

    def segments(self) -> tuple[list[StageSpec], list[StageSpec], list[StageSpec]]:
        """Split stages into (pre, body, post) around the iterate body."""
        if not self.iterate:
            return self.stages, [], []
        names = [s.name for s in self.stages]
        i = names.index(self.iterate.body[0])
        j = i + len(self.iterate.body)
        return self.stages[:i], self.stages[i:j], self.stages[j:]


@dataclass
class Defaults:
    timeout_seconds: float = 900
    max_iterations: int = 3
    caps: Caps = field(default_factory=lambda: Caps(20, 3600))
    fallback_agents: list[str] = field(default_factory=list)
    fallback_on_error: bool = True
    max_context_chars: int = 20000
    runs_dir: str = ".looper/runs"
    version_probe_timeout: float = 3
    max_agents: int = 5
    recommended_agents: int = 3
    max_parallel_local: int = 1
    max_best_of_n: int = 5
    single_agent_samples: int = 2
    chars_per_token: float = 3.5
    context_fill: float = 0.5
    unknown_local_tier: str = "medium"


HARD_MAX_AGENTS = 8  # absolute ceiling even if a config raises max_agents


@dataclass
class Policy:
    agents: dict[str, AgentSpec]
    strategies: dict[str, list[str]]
    loops: dict[str, LoopSpec]
    default_loop: str
    min_score: float
    defaults: Defaults
    source: str = "bundled"
    path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    tiers: dict[str, TierSpec] = field(default_factory=dict)
    roles: dict[str, RoleSpec] = field(default_factory=dict)
    providers: dict[str, ProviderSpec] = field(default_factory=dict)
    team: list[str] | None = None  # None = legacy: every configured agent that is available

    def role(self, name: str) -> RoleSpec:
        return self.roles.get(name) or RoleSpec(name=name, abstract=name)

    def tiers_by_rank(self) -> list[TierSpec]:
        return sorted(self.tiers.values(), key=lambda t: t.rank)

    def loop(self, name: str) -> LoopSpec:
        if name not in self.loops:
            raise ConfigError(f"Unknown loop '{name}'. Known: {', '.join(self.loops)}")
        return self.loops[name]

    def stage_preferences(self, stage: StageSpec) -> list[str]:
        """Agent preference order for a stage (explicit list, else role strategy)."""
        prefs = list(stage.agents) if stage.agents else list(self.strategies.get(stage.role, []))
        return prefs


# --------------------------------------------------------------------------- signals

def compile_signal(sig: str) -> re.Pattern[str]:
    """``re:<regex>`` -> regex; anything else -> case-insensitive substring."""
    if sig.startswith("re:"):
        return re.compile(sig[3:], re.IGNORECASE | re.MULTILINE)
    return re.compile(re.escape(sig), re.IGNORECASE)


def find_signal(text: str, signals: list[str]) -> str | None:
    for sig in signals:
        if compile_signal(sig).search(text or ""):
            return sig
    return None


# --------------------------------------------------------------------------- parsing

def _rule(raw: dict[str, Any], where: str, errors: list[str]) -> RouteRule | None:
    weight = float(raw.get("weight", 1))
    if "keyword" in raw:
        kw = str(raw["keyword"])
        pat = r"(?<![\w-])" + re.escape(kw).replace(r"\ ", r"\s+") + r"(?![\w-])"
        label = f"keyword '{kw}'"
    elif "regex" in raw:
        pat = str(raw["regex"])
        label = f"regex /{pat}/"
    else:
        errors.append(f"{where}: route rule needs 'keyword' or 'regex': {raw}")
        return None
    try:
        rx = re.compile(pat, re.IGNORECASE)
    except re.error as exc:
        errors.append(f"{where}: bad regex {pat!r}: {exc}")
        return None
    return RouteRule(pattern=pat, weight=weight, label=label, regex=rx)


def _caps(raw: dict[str, Any] | None, base: Caps) -> Caps:
    raw = raw or {}
    return Caps(
        max_total_calls=int(raw.get("max_total_calls", base.max_total_calls)),
        max_wall_seconds=float(raw.get("max_wall_seconds", base.max_wall_seconds)),
    )


def build_policy(raw: dict[str, Any], *, source: str = "dict", path: Path | None = None) -> Policy:
    """Validate a raw config dict and build a Policy. Raises ConfigError listing every problem."""
    errors: list[str] = []

    d = raw.get("defaults", {}) or {}
    defaults = Defaults(
        timeout_seconds=float(d.get("timeout_seconds", 900)),
        max_iterations=int(d.get("max_iterations", 3)),
        caps=_caps(d.get("caps"), Caps(20, 3600)),
        fallback_agents=list(d.get("fallback_agents", [])),
        fallback_on_error=bool(d.get("fallback_on_error", True)),
        max_context_chars=int(d.get("max_context_chars", 20000)),
        runs_dir=str(d.get("runs_dir", ".looper/runs")),
        version_probe_timeout=float(d.get("version_probe_timeout", 3)),
        max_agents=int(d.get("max_agents", 5)),
        recommended_agents=int(d.get("recommended_agents", 3)),
        max_parallel_local=max(1, int(d.get("max_parallel_local", 1))),
        max_best_of_n=max(1, int(d.get("max_best_of_n", 5))),
        single_agent_samples=max(1, int(d.get("single_agent_samples", 2))),
        chars_per_token=float(d.get("chars_per_token", 3.5)),
        context_fill=float(d.get("context_fill", 0.5)),
        unknown_local_tier=str(d.get("unknown_local_tier", "medium")),
    )
    if not 1 <= defaults.max_agents <= HARD_MAX_AGENTS:
        errors.append(f"defaults.max_agents must be 1..{HARD_MAX_AGENTS}")

    tiers: dict[str, TierSpec] = {}
    for tname, t in (raw.get("tiers") or {}).items():
        try:
            tiers[tname] = TierSpec(
                name=tname, rank=int(t["rank"]), max_params_b=t.get("max_params_b"),
                roles=list(t.get("roles") or []), num_ctx=t.get("num_ctx"),
                max_output_tokens=int(t.get("max_output_tokens", 4096)),
                max_context_chars=int(t.get("max_context_chars", 20000)),
                structured_output=str(t.get("structured_output", "good")),
                iteration_multiplier=float(t.get("iteration_multiplier", 1.0)),
                can_orchestrate=bool(t.get("can_orchestrate", True)),
                max_map_items=int(t.get("max_map_items", 6)), best_of_n=bool(t.get("best_of_n", True)),
                simplify_prompts=bool(t.get("simplify_prompts", False)),
                disable_thinking=bool(t.get("disable_thinking", False)),
                examples=list(t.get("examples") or []), note=str(t.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"tiers.{tname}: invalid ({exc})")
    if not tiers:  # legacy configs with inherit_defaults=false: one permissive tier
        tiers["frontier"] = TierSpec("frontier", 5, None, [], max_context_chars=10**9)
    if defaults.unknown_local_tier not in tiers:
        defaults.unknown_local_tier = max(tiers.values(), key=lambda t: t.rank).name

    roles: dict[str, RoleSpec] = {}
    for rname, r in (raw.get("roles") or {}).items():
        roles[rname] = RoleSpec(name=rname, abstract=str(r.get("abstract", rname)), lead=bool(r.get("lead", False)),
                                tools=bool(r.get("tools", False)), diverse_from=r.get("diverse_from"))

    providers: dict[str, ProviderSpec] = {}
    for pname, p in (raw.get("providers") or {}).items():
        providers[pname] = ProviderSpec(
            name=pname, kind=str(p.get("kind", "api")), api_style=p.get("api_style"), base_url=p.get("base_url"),
            api_key_env=p.get("api_key_env"), key_optional=bool(p.get("key_optional", False)),
            context_window=p.get("context_window"), models=list(p.get("models") or []), vendor=p.get("vendor"))

    agents: dict[str, AgentSpec] = {}
    for name, a in (raw.get("agents") or {}).items():
        if not isinstance(a, dict):
            errors.append(f"agents.{name}: must be an object")
            continue
        kind = str(a.get("kind", "cli"))
        if kind not in AGENT_KINDS:
            errors.append(f"agents.{name}.kind must be one of {AGENT_KINDS}")
            continue
        common = dict(
            description=str(a.get("description", "")), env={str(k): str(v) for k, v in (a.get("env") or {}).items()},
            timeout_seconds=a.get("timeout_seconds"), kind=kind, tier=a.get("tier"),
            params_b=_num(a.get("params_b")), active_params_b=_num(a.get("active_params_b")),
            context_window=a.get("context_window"), num_ctx=a.get("num_ctx"),
            max_output_tokens=a.get("max_output_tokens"), roles=list(a.get("roles") or []), tools=a.get("tools"),
        )
        if common["tier"] is not None and tiers and common["tier"] not in tiers:
            errors.append(f"agents.{name}.tier: unknown tier '{common['tier']}' (known: {', '.join(tiers)})")
        if kind == "cli":
            cmd = a.get("command") or ["{binary}", "{prompt}"]
            if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
                errors.append(f"agents.{name}.command must be a list of strings")
                continue
            via = a.get("prompt_via", "arg")
            if via not in ("arg", "stdin", "auto"):
                errors.append(f"agents.{name}.prompt_via must be 'arg', 'stdin', or 'auto'")
            if via in ("arg", "auto") and not any("{prompt}" in x for x in cmd):
                errors.append(
                    f"agents.{name}.command must contain '{{prompt}}' "
                    f"(or set prompt_via: stdin)"
                )
            agents[name] = AgentSpec(name=name, binary=str(a.get("binary", name)), command=list(cmd),
                                     version_args=list(a.get("version_args", ["--version"])), prompt_via=via,
                                     provider=a.get("provider", "cli"), **common)
            continue
        prov_name = a.get("provider")
        prov = providers.get(prov_name) if prov_name else None
        if prov_name and prov is None:
            errors.append(f"agents.{name}.provider: unknown provider '{prov_name}' (known: {', '.join(providers)})")
        if not a.get("model"):
            errors.append(f"agents.{name}: kind={kind} needs 'model'")
        style = a.get("api_style") or (prov.api_style if prov else None) or "openai"
        if style not in API_STYLES:
            errors.append(f"agents.{name}.api_style must be one of {API_STYLES}")
        base_url = a.get("base_url") or (prov.base_url if prov else None)
        if not base_url:
            errors.append(f"agents.{name}: needs 'base_url' (or a provider with one)")
        if common["context_window"] is None and prov:
            common["context_window"] = prov.context_window
        agents[name] = AgentSpec(
            name=name, provider=prov_name, model=str(a.get("model", "")), base_url=base_url,
            api_key_env=a.get("api_key_env", prov.api_key_env if prov else None),
            key_optional=bool(a.get("key_optional", prov.key_optional if prov else kind == "local")),
            api_style=style, version_args=[], **common)
    if not agents:
        errors.append("agents: at least one agent must be configured")

    for a in defaults.fallback_agents:
        if a not in agents:
            errors.append(f"defaults.fallback_agents: unknown agent '{a}'")

    strategies: dict[str, list[str]] = {}
    for role, prefs in (raw.get("strategies") or {}).items():
        if not isinstance(prefs, list):
            errors.append(f"strategies.{role}: must be a list of agent names")
            continue
        for a in prefs:
            if a not in agents:
                errors.append(f"strategies.{role}: unknown agent '{a}'")
        strategies[role] = list(prefs)

    loops: dict[str, LoopSpec] = {}
    raw_loops = raw.get("loops") or {}
    if not raw_loops:
        errors.append("loops: at least one loop must be defined")
    for lname, l in raw_loops.items():
        where = f"loops.{lname}"
        if not isinstance(l, dict):
            errors.append(f"{where}: must be an object")
            continue
        stages: list[StageSpec] = []
        seen: set[str] = set()
        for i, s in enumerate(l.get("stages") or []):
            sw = f"{where}.stages[{i}]"
            sname = s.get("name")
            if not sname:
                errors.append(f"{sw}: missing name")
                continue
            if sname in seen:
                errors.append(f"{sw}: duplicate stage name '{sname}'")
            seen.add(sname)
            stype = s.get("type", "single")
            if stype not in STAGE_TYPES:
                errors.append(f"{sw}: type must be one of {STAGE_TYPES}")
            role = s.get("role", "generalist")
            explicit = list(s.get("agents") or [])
            for a in explicit:
                if a not in agents:
                    errors.append(f"{sw}: unknown agent '{a}'")
            if not explicit and role not in strategies:
                errors.append(f"{sw}: role '{role}' has no strategy and no 'agents' list")
            if not s.get("prompt"):
                errors.append(f"{sw}: missing prompt")
            st = StageSpec(
                name=sname,
                role=role,
                prompt=str(s.get("prompt", "")),
                agents=explicit,
                type=stype,
                n=int(s.get("n", 1)),
                source=s.get("source"),
                max_items=int(s.get("max_items", 6)),
                max_workers=int(s.get("max_workers", 4)),
                timeout_seconds=s.get("timeout_seconds"),
                artifact=bool(s.get("artifact", False)),
                optional=bool(s.get("optional", False)),
                mergeable=bool(s.get("mergeable", False)),
            )
            if st.type == "parallel" and st.n < 1:
                errors.append(f"{sw}: parallel stage needs n >= 1")
            if st.type == "map":
                if not st.source or st.source not in seen - {sname}:
                    errors.append(f"{sw}: map stage needs 'source' naming an earlier stage")
            stages.append(st)
        if not stages:
            errors.append(f"{where}: needs at least one stage")

        iterate = None
        it = l.get("iterate")
        if it:
            body = list(it.get("body") or [])
            names = [s.name for s in stages]
            if not body or any(b not in names for b in body):
                errors.append(f"{where}.iterate.body: must list existing stages")
            else:
                idx = [names.index(b) for b in body]
                if idx != list(range(idx[0], idx[0] + len(idx))):
                    errors.append(f"{where}.iterate.body: stages must be contiguous and in order")
            signals = list(it.get("exit_signals") or [])
            for sig in signals:
                try:
                    compile_signal(sig)
                except re.error as exc:
                    errors.append(f"{where}.iterate.exit_signals: bad regex {sig!r}: {exc}")
            checks = list(it.get("check_stages") or body)
            for c in checks:
                if c not in body:
                    errors.append(f"{where}.iterate.check_stages: '{c}' not in body")
            iterate = IterateSpec(
                body=body,
                max_iterations=int(it.get("max_iterations", defaults.max_iterations)),
                exit_signals=signals,
                check_stages=checks,
            )
            if iterate.max_iterations < 1:
                errors.append(f"{where}.iterate.max_iterations must be >= 1")

        rules = []
        for rr in l.get("route") or []:
            rule = _rule(rr, where, errors)
            if rule:
                rules.append(rule)

        loops[lname] = LoopSpec(
            name=lname,
            description=str(l.get("description", "")),
            stages=stages,
            caps=_caps(l.get("caps"), defaults.caps),
            iterate=iterate,
            priority=int(l.get("priority", 0)),
            route=rules,
        )

    router = raw.get("router") or {}
    default_loop = router.get("default_loop", "react")
    if loops and default_loop not in loops:
        errors.append(f"router.default_loop: unknown loop '{default_loop}'")

    team = raw.get("team")
    if team is not None:
        if not isinstance(team, list) or not team:
            errors.append("team: must be a non-empty list of agent names")
            team = None
        else:
            for a in team:
                if a not in agents:
                    errors.append(f"team: unknown agent '{a}'")
            if len(set(team)) != len(team):
                errors.append("team: duplicate agent names")
            if len(team) > defaults.max_agents:
                errors.append(f"team: {len(team)} agents exceeds max_agents={defaults.max_agents} "
                              "(diminishing returns past ~3-5; see docs/MODEL_TIERS.md)")

    if errors:
        raise ConfigError("Invalid looper config:\n  - " + "\n  - ".join(errors))

    return Policy(
        agents=agents,
        strategies=strategies,
        loops=loops,
        default_loop=default_loop,
        min_score=float(router.get("min_score", 1)),
        defaults=defaults,
        source=source,
        path=path,
        raw=raw,
        tiers=tiers,
        roles=roles,
        providers=providers,
        team=list(team) if team else None,
    )


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def load_policy(explicit: Path | str | None = None) -> Policy:
    raw, label, path = load_raw_config(explicit)
    return build_policy(raw, source=label, path=path)
