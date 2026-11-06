# Changelog

## 0.7.0

- **Workspace.** `~/.looper/` (or `$LOOPER_STATE_HOME`) holds machine skills and optional `looper.json`. `<project>/.looper/` holds `AGENTS.md`, optional `SOUL.md` / `USER.md` / `MEMORY.md` / `memory/`, `roles/<role>.md`, and project skills. The project root is the nearest ancestor of the working directory that contains `.looper/`, or `--project` / request field `project`. `~/.looper/looper.json` is preferred over `~/.config/looper/looper.json` when it exists. Credentials stay in `~/.config/looper/credentials.env` (or the keyring). With no `.looper/` directory and no MCP config, prompts and tool behaviour are unchanged.
- **Skills.** `SKILL.md` (`name` + `description` frontmatter). Project skills override machine skills of the same name. The prompt receives only the name and description. The built-in `load_skill` tool is executed by looper and returns the body. `looper skills list`.
- **MCP.** `mcp.servers` in looper.json, `<project>/.looper/mcp.json`, and Claude Code `<project>/.mcp.json`. Stdio (`command`/`args`/`env`) and streamable HTTP (`url`; SSE responses accepted). `${VAR}` in env/headers/args is filled from the environment, keyring, or `credentials.env` and is never written back or placed on argv. `looper mcp list`, `looper mcp tools <server>`.
- **Approval policy.** Read-only MCP tools (`readOnlyHint: true` and not destructive) run inside the turn. Everything else comes back as `finish_reason: "approval_required"` and a `pending` list. Resume with `approvals: [{id, decision}]` on the next `ask` / `POST /v1/ask`. Precedence: deny > require_approval > allow/auto_approve > annotation default. Modes: `auto_read_only` (default), `approve_all`, `deny_all`. Per-role overrides live in `roles/*.md` frontmatter. Caller-owned tools are still never executed. The tiny tier still gets no tools.
- **CLI runners.** Claude Code receives `--append-system-prompt-file` and `--mcp-config`. Codex receives `-c project_doc_fallback_filenames` and `-c mcp_servers.*` (credential values only via the child environment). Grok and Antigravity have no per-run file flag, so `.looper/AGENTS.md` is injected into the prompt and looper hosts MCP.
- **doctor / version.** Additive `workspace`, `skills`, and `mcp` objects. Capabilities: `workspace`, `skills`, `mcp`, `mcp_approval`. `api_version` stays **1**.

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
