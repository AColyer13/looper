# Changelog

## 0.6.0

- **History trimming** for `looper ask` (opt-in, default **off**). When estimated history exceeds `context_window × budget_fill` (default 0.5), apply in order until under budget: (1) stub old Write/write_file bodies (and trivial bash heredoc writes), (2) head+tail old tool results while keeping error lines, (3) deterministic middle-collapse ledger. Pairing (`tool_call_id` / Anthropic `tool_use`↔`tool_result`) is preserved.
- Config: top-level `history_trim` in `default_config.json` + policy validation. Per-request override via `ask --request-json` / `history_trim` on the payload (`false` disables). Additive; `api_version` stays **1**.
- Response: caller-owned `messages` remain the **full untrimmed** conversation; additive `trim` report `{applied, before_tokens, after_tokens, budget, steps}`. Capability flag `history_trim`.
- Token estimate = chars / `defaults.chars_per_token` (3.5). No new dependencies.
- Docs: README "History trimming", FRONTEND_API + tool_call.schema.json, MODEL_TIERS note for local ≤32k.

## 0.5.2


- Golden tool-call fixtures from MoreThought/Fable (Apache-2.0) under `tests/fixtures/fable/` + `tests/test_golden_tool_calls.py` (parser eval only; no training use; no full traces in-repo).
- When a provider reports tool calls but every call fails validation, `finish_reason` is now `invalid_tool_calls` (additive api_version 1 enum value) instead of `stop`. Details remain in `warnings`. Applies to openai_chat / anthropic / ollama / openai_responses extractors and `json_block`.
- `looper run` context clipping: a model's declared window (`context_window` / `num_ctx` / resolved `context_tokens`) **raises** the char budget to `int(tokens × chars_per_token × context_fill)` (default fill **0.75**), instead of only tightening via `min()` against `tier.max_context_chars`. Tier ceilings remain the default when no window is declared.

## 0.5.1

- Drop duplicate committed `looper.example.json` (use `looper init` for a local copy; bundled source remains `src/looper/data/default_config.json`).
- Remove dead code: unused `Executor._max_iterations`, `EventEmitter.label_for`, unused locals/imports; single `API_VERSION` in package `__init__`.
- Dedupe CLI `extra_argv` insertion into `runners.insert_extra_argv` (avoids double-applying extras when argv is pre-built).
- Move `clip` / `render` / `parse_items` to `looper.textutil` to break the assign→executor import cycle.
- `allows()` no longer takes an unused `policy` argument.

## 0.5.0

- Structured `tool_calls` for `ask` (caller owns execution).
