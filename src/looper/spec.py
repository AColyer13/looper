"""Language-neutral team/tier spec (docs/team_spec.json) so other apps can mirror looper."""

from __future__ import annotations

from typing import Any

from looper import __version__
from looper.policy import Policy
from looper.tiers import _EFF, _MOE, _NX, _PLAIN, KNOWN_ACTIVE

ALGORITHM = [
    "1. Team = configured agents (1..max_agents). Each agent: kind cli|api|local, provider, model, tier.",
    "2. Tier: manual 'tier' wins; kind=cli or first-party API (anthropic/openai/xai/google) -> 'frontier'; "
    "else effective_params = total (dense) or sqrt(total*active) (MoE) from server metadata or the model name; "
    "pick the first tier (by rank, excluding frontier) whose max_params_b > effective_params; unknown size -> "
    "defaults.unknown_local_tier.",
    "3. For each stage: abstract = roles[stage.role].abstract. Capable = members whose tier.roles contains "
    "abstract (orchestrator also needs tier.can_orchestrate) and whose own roles list (if any) contains it.",
    "4. Order capable: roles with tools=true put tool-capable agents (CLIs) first. lead roles: higher tier rank, "
    "then strategy preference, then team order. Other roles: strategy preference, then higher rank, then team order.",
    "5. Diversity: if roles[role].diverse_from names an abstract role and the top candidate produced that role's "
    "last output, promote the next capable agent whose rank >= producer rank - 1.",
    "6. Nobody capable: optional pre/post stage -> skip; mergeable pre/post stage whose previous stage is a "
    "single stage in the same segment -> append its prompt to that stage; else assign the highest-rank member "
    "and mark under_tier.",
    "7. Lite mode: if the strongest member's tier rank <= rank('small'), skip all optional pre/post stages.",
    "8. parallel (Best-of-N): n = min(stage.n, distinct capable, max_best_of_n). One capable agent: if kind is "
    "api/local and tier.best_of_n -> n = min(stage.n, single_agent_samples) at temperatures spread 0.2..0.9; "
    "else n = 1 and the judge acts as self-critique.",
    "9. map (Orchestrator->Workers): max_items = min(stage.max_items, max tier.max_map_items of capable workers); "
    "<=3 if the orchestrator stage was under_tier; one-member team -> sequential; local workers -> at most "
    "max_parallel_local concurrent calls per local server.",
    "10. Iterations: loop max_iterations x min(tier.iteration_multiplier of body-stage agents), ceil, min 1.",
    "11. Prompts: whole prompt clipped (head+tail) to int(context_tokens*chars_per_token*context_fill) when the agent "
    "declares a window (context_window/num_ctx/context_tokens; may exceed tier.max_context_chars), else "
    "tier.max_context_chars; tiers with simplify_prompts get a short 'do one thing' prefix; text-only agents on tool roles "
    "get a 'propose diffs/commands, do not claim execution' prefix; map sources on tiers with structured_output "
    "unreliable|fair are asked for a numbered list.",
    "12. Team size: hard cap defaults.max_agents (5); warn above defaults.recommended_agents (3).",
]


def build_spec(policy: Policy) -> dict[str, Any]:
    raw = policy.raw
    return {
        "$id": "looper-team-spec",
        "spec_version": 1,
        "looper_version": __version__,
        "description": "Capability tiers, role taxonomy and the role-assignment / degradation algorithm used by "
                       "looper. Language-neutral so other apps (e.g. the-office) can mirror it.",
        "team_limits": {k: getattr(policy.defaults, k) for k in (
            "max_agents", "recommended_agents", "max_parallel_local", "max_best_of_n", "single_agent_samples",
            "chars_per_token", "context_fill", "unknown_local_tier")},
        "abstract_roles": sorted({r["abstract"] for r in raw.get("roles", {}).values()} |
                                 {x for t in raw.get("tiers", {}).values() for x in t.get("roles", [])}),
        "roles": raw.get("roles", {}),
        "tiers": raw.get("tiers", {}),
        "effective_params": {"dense": "total_params_b", "moe": "sqrt(total_params_b * active_params_b)",
                             "note": "heuristic; parameter count is a weak predictor, allow manual override"},
        "name_parsing": {
            "units": {"m": 0.001, "b": 1, "t": 1000},
            "known_moe": {k: {"total_b": v[0], "active_b": v[1]} for k, v in KNOWN_ACTIVE.items()},
            "regex_order": [
                {"kind": "moe", "pattern": _MOE.pattern, "example": "qwen3:30b-a3b -> 30B total, 3B active"},
                {"kind": "n_x_m", "pattern": _NX.pattern, "example": "mixtral:8x7b -> 56B total, 14B active"},
                {"kind": "effective", "pattern": _EFF.pattern, "example": "gemma4:e4b -> 4B"},
                {"kind": "plain", "pattern": _PLAIN.pattern, "example": "qwen3.5:9b -> 9B (last match wins)"},
            ],
            "flags": "case-insensitive",
        },
        "detection": {
            "ollama": {"list": "GET {base}/api/tags -> models[].name, details.parameter_size, quantization_level",
                       "show": "POST {base}/api/show {model} -> model_info['general.parameter_count'], "
                               "model_info['<general.architecture>.context_length'], <arch>.expert_count (MoE)",
                       "runtime_context": "request options.num_ctx (Ollama's own default depends on VRAM: "
                                          "4k <24GiB, 32k 24-48GiB, 256k >=48GiB)"},
            "lmstudio": {"list": "GET {root}/api/v1/models -> models[].key, params_string, max_context_length, "
                                 "quantization.name; fallback GET {root}/api/v0/models -> data[].id, "
                                 "max_context_length, quantization"},
            "vllm": {"list": "GET {base}/models -> data[].id, max_model_len"},
            "llama_cpp": {"list": "GET {base}/models -> data[].id, meta.n_params, meta.n_ctx_train"},
        },
        "providers": {n: {k: v for k, v in p.items()} for n, p in raw.get("providers", {}).items()},
        "credentials": {"storage": "env var > OS keyring (optional) > <LOOPER_HOME|~/.config/looper>/credentials.env "
                                   "(mode 0600)", "never_in_config": True, "display": "mask all but last 4 chars"},
        "assignment_algorithm": ALGORITHM,
        "degradation_order": ["skip optional", "merge into previous single stage", "under_tier on strongest member"],
        "stage_flags": {"optional": "may be skipped", "mergeable": "may be folded into the previous stage"},
    }
