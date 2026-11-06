"""Router: apply the policy's weighted keyword/regex rules to a prompt."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from looper.policy import Policy


@dataclass
class LoopScore:
    loop: str
    score: float
    matched: list[str] = field(default_factory=list)


@dataclass
class RouteDecision:
    loop: str
    score: float
    method: str  # "override" | "rules" | "default"
    reason: str
    ranking: list[LoopScore] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def score_loops(prompt: str, policy: Policy) -> list[LoopScore]:
    order = {name: i for i, name in enumerate(policy.loops)}
    scores: list[LoopScore] = []
    for name, loop in policy.loops.items():
        s = LoopScore(loop=name, score=0.0)
        for rule in loop.route:
            if rule.matches(prompt):
                s.score += rule.weight
                s.matched.append(f"{rule.label} (+{rule.weight:g})")
        scores.append(s)
    scores.sort(key=lambda s: (-s.score, -policy.loops[s.loop].priority, order[s.loop]))
    return scores


def route(prompt: str, policy: Policy, *, override: str | None = None) -> RouteDecision:
    ranking = score_loops(prompt, policy)
    if override:
        policy.loop(override)  # raises on unknown
        return RouteDecision(override, 0.0, "override", f"--loop {override} given explicitly", ranking)
    top = ranking[0] if ranking else None
    if top and top.score >= policy.min_score:
        tie = [r.loop for r in ranking[1:] if r.score == top.score]
        reason = f"score {top.score:g} from " + ", ".join(top.matched)
        if tie:
            reason += f"; tie with {', '.join(tie)} broken by priority"
        return RouteDecision(top.loop, top.score, "rules", reason, ranking)
    best = f"best was {top.loop}={top.score:g}" if top and top.score else "no rules matched"
    return RouteDecision(
        policy.default_loop, top.score if top else 0.0, "default",
        f"{best} (< min_score {policy.min_score:g}); using default loop '{policy.default_loop}'",
        ranking,
    )
