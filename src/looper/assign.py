"""Team-aware role assignment and graceful degradation.

Given a loop and a team of 1..max_agents agents with capability tiers, decide
for every stage: which agent runs it (and the fallback order), how much context
it gets, and what to change when nobody on the team can do the stage well.

Algorithm (mirrored language-neutrally in docs/team_spec.json):

1. Map the stage's concrete role (``coder``, ``grader``...) to an abstract role
   (``coder``, ``judge``...) via ``roles``.
2. Capable agents = team members whose tier allows the abstract role (and whose
   own ``roles`` list, if set, includes it). ``orchestrator`` also needs a tier
   with ``can_orchestrate``.
3. Order capable agents:
   * tool roles (edit files / run commands): agents with tools (CLIs) first
   * lead roles (planner, judge, reviewer, orchestrator): strongest tier first,
     then the role's strategy preference
   * other roles: strategy preference first, then strongest tier
   * diversity: a reviewer/judge avoids the agent that produced the work it
     checks when another capable agent of similar tier exists (N >= 2)
4. Nobody capable -> degrade, in this order: skip if ``optional`` (pre/post only);
   merge into the previous single stage if ``mergeable``; else run on the
   strongest available agent and flag ``under_tier``.
5. Lite mode (best agent on the team is tiny/small): optional pre/post stages are skipped.
6. Best-of-N: n = min(stage n, distinct capable agents, max_best_of_n). With one
   agent: an api/local model of a tier that allows it samples
   ``single_agent_samples`` times at spread temperatures; otherwise collapse to
   one candidate and the judge acts as a self-critique.
7. Orchestrator->Workers: items capped by the worker tier's ``max_map_items``; fan-out
   runs sequentially with one agent or any local agent (``max_parallel_local``).
8. Iterations: loop max x the weakest body agent's ``iteration_multiplier`` (min 1).
"""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass, field, replace
from typing import Any

from looper.credentials import get_key
from looper.policy import AgentSpec, IterateSpec, LoopSpec, Policy, RoleSpec, StageSpec, TierSpec
from looper.tiers import TierInfo, resolve_tier

SMALL_PREFIX = ("Keep your answer short and concrete. Do only the step asked below. "
                "If something is unclear, say so instead of guessing.\n\n")
TEXT_ONLY_PREFIX = ("NOTE: you cannot read files, edit files or run commands in this setting. Work only from the "
                    "text below. Give the exact changes (unified diff or full file contents) and the exact "
                    "commands a human should run; do not claim you ran anything.\n\n")
LIST_SUFFIX = "\n\nFormat: a plain numbered list, one item per line, nothing else."
MERGE_GLUE = "\n\n---\nTHEN, in the same answer, also do this next step:\n"


@dataclass
class Member:
    name: str
    agent: AgentSpec
    tier: TierSpec
    info: TierInfo
    endpoint: str
    vendor: str = ""

    @property
    def rank(self) -> int:
        return self.tier.rank

    def to_dict(self) -> dict[str, Any]:
        a = self.agent
        return {"name": self.name, "kind": a.kind, "provider": a.provider, "model": a.model or a.binary,
                "tier": self.tier.name, "size": self.info.size, "tier_source": self.info.source,
                "context_tokens": self.info.context_tokens, "tools": a.has_tools}


@dataclass
class StagePlan:
    stage: StageSpec
    segment: str
    order: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | under_tier | skipped | merged | no_agent
    merged_into: str | None = None
    merged: list[str] = field(default_factory=list)
    temperatures: list[float] | None = None
    notes: list[str] = field(default_factory=list)
    text_only: bool = False
    list_output: bool = False

    @property
    def agent(self) -> str | None:
        return self.order[0] if self.order else None


@dataclass
class Adaptation:
    loop: LoopSpec  # adapted: skipped/merged stages removed, n/max_items/max_workers adjusted
    stages: dict[str, StagePlan]
    max_iterations: int
    members: dict[str, Member]
    degradations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # merged stage -> host stage

    def dropped(self) -> list[StagePlan]:
        return [p for p in self.stages.values() if p.status in ("skipped", "merged")]


# --------------------------------------------------------------------------- availability

def agent_endpoint(agent: AgentSpec) -> tuple[str | None, str]:
    """(endpoint or None, reason). cli -> PATH lookup; api -> key present; local -> base_url."""
    if agent.kind == "cli":
        from looper.detect import resolve_binary
        path = shutil.which(os.path.expanduser(resolve_binary(agent)))
        return (path, "installed") if path else (None, f"`{agent.binary}` not on PATH")
    if agent.kind == "api" and agent.api_key_env and not agent.key_optional and not get_key(agent.api_key_env):
        return None, f"missing ${agent.api_key_env} (run `looper keys set {agent.provider}`)"
    return agent.base_url or "", "configured"


def default_available(policy: Policy) -> tuple[dict[str, str], dict[str, str]]:
    names = policy.team or list(policy.agents)
    avail, missing = {}, {}
    for n in names:
        ep, why = agent_endpoint(policy.agents[n])
        if ep is not None:
            avail[n] = ep
        else:
            missing[n] = why
    return avail, missing


def build_members(policy: Policy, available: dict[str, str], force_agent: str | None = None) -> list[Member]:
    order = [force_agent] if force_agent else (policy.team or list(policy.agents))
    order = order + [n for n in available if n not in order and not policy.team and not force_agent]
    out = []
    for n in order:
        if n in available and n in policy.agents:
            a = policy.agents[n]
            info = resolve_tier(a, policy)
            prov = policy.providers.get(a.provider or "")
            vendor = n if a.kind == "cli" else ((prov.vendor if prov else None) or a.provider or n)
            out.append(Member(n, a, policy.tiers[info.tier], info, available[n], vendor))
    return out


# --------------------------------------------------------------------------- helpers

def allows(m: Member, role: RoleSpec) -> bool:
    abstract = role.abstract
    if m.tier.roles and abstract not in m.tier.roles:
        return False
    if abstract == "orchestrator" and not m.tier.can_orchestrate:
        return False
    if m.agent.roles and abstract not in m.agent.roles and role.name not in m.agent.roles:
        return False
    return True


def _pref(policy: Policy, stage: StageSpec) -> list[str]:
    seen: list[str] = []
    for a in policy.stage_preferences(stage) + policy.defaults.fallback_agents:
        if a not in seen:
            seen.append(a)
    return seen


def order_members(members: list[Member], role: RoleSpec, pref: list[str]) -> list[Member]:
    big = len(pref) + 1
    idx = {m.name: i for i, m in enumerate(members)}

    def key(m: Member) -> tuple:
        if m.name in pref:
            p: float = pref.index(m.name)
        elif m.vendor in pref:  # e.g. an xAI API model inherits grok's place in the strategy
            p = pref.index(m.vendor) + 0.5
        else:
            p = big
        tools = 0 if (not role.tools or m.agent.has_tools) else 1
        if role.lead:
            return (tools, -m.rank, p, idx[m.name])
        return (tools, p, -m.rank, idx[m.name])
    return sorted(members, key=key)


def context_budget(m: Member, policy: Policy) -> tuple[int, int]:
    """(per-variable char limit, whole-prompt char limit) for this agent."""
    whole = m.tier.max_context_chars
    if m.info.context_tokens:
        whole = min(whole, int(m.info.context_tokens * policy.defaults.chars_per_token * policy.defaults.context_fill))
    per_var = min(policy.defaults.max_context_chars, whole)
    return max(per_var, 500), max(whole, 1000)


def max_tokens_for(m: Member) -> int:
    return int(m.agent.max_output_tokens or m.tier.max_output_tokens)


def adapt_prompt(prompt: str, m: Member, plan: StagePlan | None, policy: Policy) -> str:
    from looper.textutil import clip
    _, whole = context_budget(m, policy)
    prefix = ""
    if plan and plan.text_only and not m.agent.has_tools:
        prefix += TEXT_ONLY_PREFIX
    if m.tier.simplify_prompts:
        prefix += SMALL_PREFIX
    suffix = LIST_SUFFIX if (plan and plan.list_output and m.tier.structured_output in ("unreliable", "fair")) else ""
    room = max(500, whole - len(prefix) - len(suffix))
    return prefix + clip(prompt, room) + suffix


# --------------------------------------------------------------------------- adapt

def _pinned_agent(assign: dict[str, str] | None, role: RoleSpec, stage_role: str) -> str | None:
    """Resolve an assign-json pin: concrete role name, then abstract role."""
    if not assign:
        return None
    return assign.get(stage_role) or assign.get(role.name) or assign.get(role.abstract)


def adapt(policy: Policy, loop: LoopSpec, members: list[Member], *, force_agent: str | None = None,
          max_iterations: int | None = None, assign: dict[str, str] | None = None) -> Adaptation:
    by_name = {m.name: m for m in members}
    pre, body, post = loop.segments()
    seg_of = {s.name: "pre" for s in pre} | {s.name: "loop" for s in body} | {s.name: "post" for s in post}
    plans: dict[str, StagePlan] = {}
    degr: list[str] = []
    warn: list[str] = []
    best_rank = max((m.rank for m in members), default=-1)
    small_rank = policy.tiers["small"].rank if "small" in policy.tiers else -1
    lite = bool(members) and best_rank <= small_rank and not force_agent
    if lite:
        degr.append(f"lite mode: strongest agent is tier '{max(members, key=lambda m: m.rank).tier.name}'; "
                    "optional stages skipped, iterations reduced")
    producer: dict[str, str] = {}  # abstract role -> last agent that did it
    map_sources = {s.source for s in loop.stages if s.type == "map" and s.source}
    prev_single: str | None = None

    for s in loop.stages:
        role = policy.role(s.role)
        seg = seg_of[s.name]
        sp = StagePlan(stage=replace(s), segment=seg)
        plans[s.name] = sp
        sp.list_output = s.name in map_sources
        pref = _pref(policy, s)
        pinned = None if force_agent else _pinned_agent(assign, role, s.role)

        if force_agent:
            cands = members[:]
            if cands and not allows(cands[0], role):
                sp.status = "under_tier"
                sp.notes.append(f"--agent {force_agent} (tier {cands[0].tier.name}) is below the tier for role "
                                f"'{role.abstract}'")
        elif pinned:
            if pinned not in by_name:
                warn.append(f"assign pin '{pinned}' for role '{s.role}' is not on the ready team; falling back")
                capable = [m for m in members if allows(m, role)]
                cands = order_members(capable, role, pref) or order_members(
                    members, RoleSpec(role.name, role.abstract, lead=True, tools=role.tools), pref)
                if cands and not allows(cands[0], role):
                    sp.status = "under_tier"
            else:
                pin_m = by_name[pinned]
                cands = [pin_m] + [m for m in members if m.name != pinned]
                sp.notes.append(f"pinned by assign-json to {pinned}")
                if not allows(pin_m, role):
                    sp.status = "under_tier"
                    need = _min_tier_for(role, policy)
                    warn.append(f"assign pin '{pinned}' (tier {pin_m.tier.name}) is under-tier for "
                                f"role '{s.role}' (needs ≥{need})")
                    sp.notes.append(f"under-tier pin (needs ≥{need})")
        else:
            capable = [m for m in members if allows(m, role)]
            cands = order_members(capable, role, pref)
            if lite and s.optional and seg != "loop":
                sp.status = "skipped"
                degr.append(f"skipped optional stage '{s.name}' (lite mode)")
                continue
            if not cands and members:
                if s.optional and seg != "loop":
                    sp.status = "skipped"
                    degr.append(f"skipped optional stage '{s.name}': no agent may act as '{role.abstract}'")
                    continue
                host = plans.get(prev_single) if prev_single else None
                if s.mergeable and host and host.segment == seg and seg != "loop" and host.stage.type == "single":
                    sp.status, sp.merged_into = "merged", host.stage.name
                    host.merged.append(s.name)
                    host.stage = replace(host.stage, prompt=host.stage.prompt + MERGE_GLUE + _strip_ctx(
                        s.prompt, host.stage.name))
                    host.list_output = host.list_output or sp.list_output
                    degr.append(f"merged '{s.name}' into '{host.stage.name}': no agent may act as '{role.abstract}'")
                    continue
                cands = order_members(members, RoleSpec(role.name, role.abstract, lead=True, tools=role.tools), pref)
                sp.status = "under_tier"
                need = _min_tier_for(role, policy)
                degr.append(f"'{s.name}' ({role.abstract}) runs on {cands[0].name} [tier {cands[0].tier.name}]"
                            f" - below recommended tier '{need}'")
                sp.notes.append(f"under-tier (needs ≥{need})")
            # diversity: checker != producer
            if role.diverse_from and len(cands) > 1:
                prod = producer.get(role.diverse_from)
                if prod and cands[0].name == prod:
                    pr = by_name[prod]
                    ok = [m for m in cands[1:] if m.rank >= pr.rank - 1]
                    alt = next((m for m in ok if m.vendor != pr.vendor), None) or (ok[0] if ok else None)
                    if alt:
                        cands = [alt] + [m for m in cands if m is not alt]
                        sp.notes.append(f"diversity: checks {prod}'s work")
        sp.order = [m.name for m in cands]
        if not cands:
            sp.status = "no_agent"
            continue
        top = cands[0]
        producer[role.abstract] = top.name
        if role.tools and not top.agent.has_tools:
            sp.text_only = True
            sp.notes.append("text-only: proposes edits/commands")

        if s.type == "parallel":
            _adapt_parallel(sp, cands, policy, degr, force_agent)
            cands = [by_name[n] for n in sp.order]
        elif s.type == "map":
            _adapt_map(sp, cands, members, plans, policy, degr)
        if s.type == "single":
            prev_single = s.name
        else:
            prev_single = None

    # iterations
    max_it = 0
    if loop.iterate:
        base = loop.iterate.max_iterations
        if max_iterations:
            max_it = max_iterations
        else:
            mults = [by_name[plans[b].agent].tier.iteration_multiplier for b in loop.iterate.body
                     if plans[b].agent in by_name]
            mult = min(mults) if mults else 1.0
            max_it = max(1, math.floor(base * mult + 0.5))
            if max_it != base:
                degr.append(f"max_iterations {base} -> {max_it} (weakest body agent multiplier ×{mult:g})")

    # warnings
    n = len(members)
    if n > policy.defaults.recommended_agents and not force_agent:
        warn.append(f"team has {n} agents; returns diminish past {policy.defaults.recommended_agents} "
                    f"(hard cap {policy.defaults.max_agents})")
    tool_stages = [p.stage.name for p in plans.values() if p.text_only]
    if tool_stages and not any(m.agent.has_tools for m in members):
        warn.append("no team member can read files or run commands (no CLI agent); tool stages "
                    f"({', '.join(tool_stages)}) return proposals for you to apply")
    if any(m.agent.kind == "local" for m in members):
        warn.append(f"local models run at most {policy.defaults.max_parallel_local} call(s) at a time per server "
                    "(GPU contention); set defaults.max_parallel_local to change")

    kept = [p.stage for p in plans.values() if p.status not in ("skipped", "merged")]
    it = loop.iterate
    new_iter = IterateSpec(it.body, max_it, it.exit_signals, it.check_stages) if it else None
    aliases = {p.stage.name: p.merged_into for p in plans.values() if p.merged_into}
    return Adaptation(loop=replace(loop, stages=kept, iterate=new_iter), stages=plans, max_iterations=max_it,
                      members=by_name, degradations=degr, warnings=warn, aliases=aliases)


def _strip_ctx(prompt: str, host: str) -> str:
    p = prompt
    if p.startswith("TASK:\n{{task}}\n\n"):
        p = p[len("TASK:\n{{task}}\n\n"):]
    return p.replace("{{outputs." + host + "}}", "(your notes above)")


def _min_tier_for(role: RoleSpec, policy: Policy) -> str:
    for t in policy.tiers_by_rank():
        if not t.roles or role.abstract in t.roles:
            if role.abstract != "orchestrator" or t.can_orchestrate:
                return t.name
    return policy.tiers_by_rank()[-1].name


def _adapt_parallel(sp: StagePlan, cands: list[Member], policy: Policy, degr: list[str], force: str | None) -> None:
    s = sp.stage
    cap = policy.defaults.max_best_of_n
    distinct = len(cands)
    # different vendors first: correlated models add little (see docs/MODEL_TIERS.md)
    firsts, rest, seen = [], [], set()
    for m in cands:
        (rest if m.vendor in seen else firsts).append(m)
        seen.add(m.vendor)
    cands[:] = firsts + rest
    sp.order = [m.name for m in cands]
    top = cands[0]
    if distinct >= 2:
        n = min(s.n, distinct, cap)
    elif top.agent.kind in ("api", "local") and top.tier.best_of_n:
        n = min(s.n, policy.defaults.single_agent_samples, cap)
        if n > 1:
            sp.temperatures = [round(0.2 + 0.7 * i / (n - 1), 2) for i in range(n)]
            sp.notes.append(f"one model sampled {n}x at temperatures {sp.temperatures}")
    else:
        n = 1
        why = "CLI agent can't vary sampling" if top.agent.kind == "cli" else f"tier '{top.tier.name}' too small"
        sp.notes.append("collapsed to a single candidate; the judge acts as self-critique")
        degr.append(f"best-of-n '{s.name}': 1 candidate instead of {s.n} ({why}); judge = self-critique")
    if 1 < n < s.n:
        degr.append(f"best-of-n '{s.name}': {n} candidates instead of {s.n} (team has {distinct} capable agent(s))")
    if any(m.agent.kind == "local" for m in cands[:n]):
        sp.notes.append(f"local candidates run {policy.defaults.max_parallel_local} at a time")
    sp.stage = replace(s, n=n)


def _adapt_map(sp: StagePlan, cands: list[Member], members: list[Member], plans: dict[str, StagePlan],
               policy: Policy, degr: list[str]) -> None:
    s = sp.stage
    tier_cap = max((m.tier.max_map_items for m in cands), default=s.max_items) or 1
    items = min(s.max_items, tier_cap)
    src = plans.get(s.source or "")
    if src and src.status == "under_tier":
        items = min(items, 3)
    if items < s.max_items:
        degr.append(f"map '{s.name}': at most {items} subtasks instead of {s.max_items}")
    workers = s.max_workers
    if len(members) == 1:
        workers = 1
        degr.append(f"map '{s.name}': subtasks run sequentially on {cands[0].name} (single-agent team)")
    elif any(m.agent.kind == "local" for m in cands):
        workers = min(workers, policy.defaults.max_parallel_local * max(1, sum(
            1 for m in cands if m.agent.kind != "local")) or 1)
        if workers < s.max_workers:
            sp.notes.append(f"fan-out limited to {workers} concurrent call(s) (local GPU)")
    sp.stage = replace(s, max_items=items, max_workers=max(1, workers))
