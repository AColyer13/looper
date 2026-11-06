"""Capability tiers: estimate a model's size and map it to a tier with limits.

Size sources, best first: manual ``tier`` on the agent > ``params_b`` /
``active_params_b`` (from Ollama ``/api/show``, LM Studio ``params_string``,
llama.cpp ``meta.n_params``) > parsing the model name (``qwen3:8b``,
``Qwen3.5-35B-A3B``, ``gemma3:270m``, ``mixtral:8x7b``, ``gemma4:e4b``).

Effective size: dense -> total params; MoE -> sqrt(total * active), a common
rule of thumb (a 35B-A3B model behaves roughly like a ~10B dense model). It is
a heuristic: parameter count is a weak predictor of agent quality, so every
tier can be overridden per agent.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

from looper.policy import AgentSpec, Policy, TierSpec

# Known MoE active-parameter counts whose names don't carry an A<n>B suffix.
KNOWN_ACTIVE = {
    "gpt-oss-20b": (21.0, 3.6), "gpt-oss:20b": (21.0, 3.6),
    "gpt-oss-120b": (117.0, 5.1), "gpt-oss:120b": (117.0, 5.1),
    "llama4:scout": (109.0, 17.0), "llama-4-scout": (109.0, 17.0),
    "llama4:maverick": (400.0, 17.0), "llama-4-maverick": (400.0, 17.0),
    "qwen3.8-flash-next": (125.0, 6.0),
}

_UNIT = {"m": 1e-3, "b": 1.0, "t": 1e3}
# 30b-a3b / 35B-A3B / 2.4t-a95b / 122B-A10B
_MOE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([bt])[-_]?a(\d+(?:\.\d+)?)\s*([bm])(?![a-z])", re.I)
# 8x7b (mixtral-style)
_NX = re.compile(r"(?<![\w.])(\d+)x(\d+(?:\.\d+)?)b(?![a-z])", re.I)
# gemma "e4b" = effective 4B
_EFF = re.compile(r"(?<![a-z0-9])e(\d+(?:\.\d+)?)b(?![a-z])", re.I)
# plain 8b / 0.6B / 270m / 1.5t   (not preceded by a letter/digit: avoids q4_k_m, v3, x7b)
_PLAIN = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*([mbt])(?![a-z0-9])", re.I)


@dataclass
class SizeInfo:
    total_b: float | None
    active_b: float | None
    source: str  # manual | metadata | name | known | unknown

    @property
    def effective_b(self) -> float | None:
        if self.total_b is None:
            return None
        if self.active_b and self.active_b < self.total_b:
            return math.sqrt(self.total_b * self.active_b)
        return self.total_b

    def label(self) -> str:
        if self.total_b is None:
            return "hosted" if self.source == "hosted" else "?"
        t = _fmt_b(self.total_b)
        return f"{t} (A{_fmt_b(self.active_b)})" if self.active_b and self.active_b < self.total_b else t


def _fmt_b(v: float | None) -> str:
    if v is None:
        return "?"
    if v < 1:
        return f"{v * 1000:.0f}M"
    if v >= 1000:
        return f"{v / 1000:.1f}T"
    return f"{v:.1f}B".replace(".0B", "B")


def parse_params(name: str) -> SizeInfo:
    """Parse total/active params (billions) from a model name or tag."""
    if not name:
        return SizeInfo(None, None, "unknown")
    low = name.lower().strip()
    for key, (tot, act) in KNOWN_ACTIVE.items():
        if key in low:
            return SizeInfo(tot, act, "known")
    m = _MOE.search(low)
    if m:
        tot = float(m.group(1)) * _UNIT[m.group(2).lower()]
        act = float(m.group(3)) * _UNIT[m.group(4).lower()]
        return SizeInfo(tot, act, "name")
    m = _NX.search(low)
    if m:
        n, each = int(m.group(1)), float(m.group(2))
        return SizeInfo(n * each, min(2, n) * each, "name")  # top-2 routing typical
    m = _EFF.search(low)
    if m:
        return SizeInfo(float(m.group(1)), None, "name")
    best = None
    for m in _PLAIN.finditer(low):
        v = float(m.group(1)) * _UNIT[m.group(2).lower()]
        if 0.01 <= v <= 5000:
            best = v  # last size token wins (e.g. "llama3.1:8b" -> 8)
    return SizeInfo(best, None, "name") if best is not None else SizeInfo(None, None, "unknown")


def parse_size_string(s: str | None) -> float | None:
    """'8.0B' / '270M' / '7B' / '1.2T' -> billions."""
    if not s:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([kmbt])?", str(s), re.I)
    if not m:
        return None
    unit = (m.group(2) or "b").lower()
    return float(m.group(1)) * {"k": 1e-6, **_UNIT}[unit]


def tier_for_size(effective_b: float, policy: Policy) -> TierSpec:
    ordered = [t for t in policy.tiers_by_rank() if t.name != "frontier"] or policy.tiers_by_rank()
    for t in ordered:
        if t.max_params_b is not None and effective_b < float(t.max_params_b):
            return t
    return ordered[-1]


def agent_size(agent: AgentSpec) -> SizeInfo:
    if agent.params_b:
        return SizeInfo(agent.params_b, agent.active_params_b, "metadata")
    if agent.kind == "cli":
        return SizeInfo(None, None, "hosted")
    return parse_params(agent.model or agent.name)


@dataclass
class TierInfo:
    tier: str
    rank: int
    size: str
    effective_b: float | None
    source: str  # manual | frontier-default | size:<metadata|name|known> | unknown-default
    context_tokens: int | None  # usable context (min of model window and requested num_ctx)

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_tier(agent: AgentSpec, policy: Policy) -> TierInfo:
    size = agent_size(agent)
    if agent.tier and agent.tier in policy.tiers:
        t, src = policy.tiers[agent.tier], "manual"
    elif agent.kind == "cli" or (agent.kind == "api" and agent.provider in _FIRST_PARTY):
        t = policy.tiers.get("frontier") or max(policy.tiers.values(), key=lambda x: x.rank)
        src = "frontier-default"
        if agent.kind == "api" and size.source == "unknown":
            size = SizeInfo(None, None, "hosted")
    elif size.effective_b is not None:
        t, src = tier_for_size(size.effective_b, policy), f"size:{size.source}"
    else:
        t, src = policy.tiers[policy.defaults.unknown_local_tier], "unknown-default"
    return TierInfo(t.name, t.rank, size.label(), size.effective_b, src, usable_context(agent, t))


_FIRST_PARTY = {"anthropic", "openai", "xai", "google"}


def usable_context(agent: AgentSpec, tier: TierSpec) -> int | None:
    """Tokens we will actually use: min(model window, requested num_ctx)."""
    want = agent.num_ctx or (tier.num_ctx if agent.kind == "local" else None)
    vals = [v for v in (agent.context_window, want) if v]
    return int(min(vals)) if vals else None
