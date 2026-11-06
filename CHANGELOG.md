# Changelog

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
