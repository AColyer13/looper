# Changelog

## 0.5.1

- Drop duplicate committed `looper.example.json` (use `looper init` for a local copy; bundled source remains `src/looper/data/default_config.json`).
- Remove dead code: unused `Executor._max_iterations`, `EventEmitter.label_for`, unused locals/imports; single `API_VERSION` in package `__init__`.
- Dedupe CLI `extra_argv` insertion into `runners.insert_extra_argv` (avoids double-applying extras when argv is pre-built).
- Move `clip` / `render` / `parse_items` to `looper.textutil` to break the assign→executor import cycle.
- `allows()` no longer takes an unused `policy` argument.

## 0.5.0

- Structured `tool_calls` for `ask` (caller owns execution).
