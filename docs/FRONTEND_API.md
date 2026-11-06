# looper Frontend API (api_version 1, package 0.7.0)

Stable, versioned machine interface for apps that sit on top of looper
(e.g. a themed UI). **looper stays persona-agnostic** — frontends may attach
their own labels (character names, desk ids) via passthrough fields; looper
echoes them but never interprets them.

Prefer this contract over scraping human CLI output. Bump `api_version` only
on breaking changes; additive fields are fine within the same version.

## Compatibility

```bash
looper version --json
```

```json
{
  "name": "looper",
  "version": "0.7.0",
  "api_version": 1,
  "capabilities": [
    "version", "doctor", "plan", "run_events", "ask", "ask_stdin",
    "ask_tools", "tool_calls", "tool_results", "history_trim", "serve",
    "team_override", "assign_override", "role_labels", "meta",
    "keys", "models_discover",
    "workspace", "skills", "mcp", "mcp_approval"
  ]
}
```

A frontend should refuse to start (or show a clear install prompt) when:

* `looper` is not on `PATH`, or
* `api_version` is missing / greater than what the frontend understands, or
* `looper doctor --json` exits non-zero (`usable: false`).

## Commands

| Command | Purpose | Exit codes |
|---|---|---|
| `looper version [--json]` | Name, version, api_version, capabilities | 0 |
| `looper doctor [--json]` | Config, team readiness, CLIs, keys (masked), local servers, problems | 0 usable / 1 not |
| `looper plan [--json] "task"` | Route + adapt; stage list with agents; no execution | 0 / 2 bad input |
| `looper plan --stdin` / `--prompt-file PATH` | Same; long tasks without argv limits | 0 / 2 |
| `looper run --json-events "task"` | NDJSON event stream on **stdout**; human logs on **stderr** | 0 ok/capped, 1 failed, 130 cancelled, 2 bad input |
| `looper run --json-events --stdin` / `--prompt-file PATH` | Same; long tasks without argv limits | same |
| `looper ask --json --agent NAME "prompt"` | One prompt → one agent | 0 / 1 / 2 |
| `looper ask --json --agent NAME --stdin` | Same; prompt on stdin (no argv length limit) | 0 / 1 / 2 |
| `looper ask --json --agent NAME --prompt-file PATH` | Same; prompt from a file | 0 / 1 / 2 |
| `looper ask --json --agent NAME --request-json -` | Full request (tools/messages/tool_results) on stdin | 0 / 1 / 2 |
| `looper ask --json --agent NAME --tools-json '[...]' "prompt"` | Offer caller-owned tools; response may include tool_calls | 0 / 1 / 2 |
| `looper serve --host 127.0.0.1 --port 8765` | Local HTTP API (same shapes) | runs until SIGINT |

Shared flags (plan / run / ask / doctor where relevant):

* `--config PATH` / `$LOOPER_CONFIG`
* `--team-json '{...}'` / `--team-file PATH` — drive the team without editing `~/.config/looper/looper.json`
* `--assign-json '{"planner":"codex","reviewer":"claude"}'` — pin roles to agents (validated; under-tier → warning)
* `--role-labels-json '{"planner":"Michael"}'` — opaque labels echoed in plan/events
* `--meta key=value` (repeatable) / `--meta-json '{...}'` — opaque metadata echoed in events
* `--loop NAME`, `--agent NAME`, `--max-iterations N`, `--cwd DIR`
* `--project DIR` — project root (directory that contains `.looper/`). Otherwise looper walks upward from `--cwd` or the process cwd. Additive; omit it and behaviour matches 0.6 when no `.looper/` directory is present.

**Keys never go on argv.** Pass them via the process environment or
`looper keys set` / `PUT /v1/keys/{provider}`. Team JSON must not include key values.

### Team override schema

```json
{
  "agents": {
    "sonnet": {"kind": "api", "provider": "anthropic", "model": "claude-sonnet-5-5"},
    "codex": {"kind": "cli", "binary": "codex", "command": ["{binary}", "{prompt}"]}
  },
  "team": ["sonnet", "codex"]
}
```

Shorthand: a list of agent objects (each with `name`) or a list of existing agent names.

## NDJSON event stream (`--json-events` / `POST /v1/run`)

One JSON object per line, flushed immediately (line-buffered). Human progress
stays on stderr. Event schema: [`events.schema.json`](./events.schema.json).

| Event | When | Notable fields |
|---|---|---|
| `run_started` | Run begins | `loop`, `task`, `team`, `degradations`, `assign`, `meta`, `role_labels` |
| `routed` | After routing | `loop`, `method`, `reason`, `score`, `ranking` |
| `stage_started` | Before a stage call | `stage`, `role`, `abstract_role`, `agent`, `label`, `iteration`, `type` |
| `stage_output` | After a stage produces text | `stage`, `text`, `truncated`, `agent`, `iteration` |
| `stage_finished` | Stage done | `stage`, `status` (`ok`/`failed`), `duration`, `exit_signal`, `error` |
| `degraded` | Adaptation message | `message` |
| `warning` | Soft problems (under-tier, caps) | `message` |
| `run_finished` | Terminal | `status` (`ok`/`failed`/`capped`/`cancelled`), `final_output`, `transcript_path`, `stop_reason` |
| `error` | Hard failure or cancel | `message`, `status` |

`agent` objects look like:

```json
{"name": "codex", "kind": "cli", "provider": "cli", "model": "codex", "tier": "frontier", "size": "frontier", "tools": true}
```

### Cancellation

* CLI: `SIGINT` / `SIGTERM` → kill child CLI subprocesses → emit `run_finished` with `status: "cancelled"` (exit 130).
* HTTP: `POST /v1/runs/{id}/cancel` (run id is in `meta.run_id` and the `X-Looper-Run-Id` response header).

### Example (Node)

```js
const { spawn } = require("child_process");
const child = spawn("looper", [
  "run", "--json-events",
  "--role-labels-json", JSON.stringify({ planner: "Michael", coder: "Dwight" }),
  "--meta", "desk=conference",
  "implement a rate limiter",
], { env: process.env });

let buf = "";
child.stdout.on("data", (chunk) => {
  buf += chunk.toString();
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i); buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    const ev = JSON.parse(line);
    // ev.event === "stage_output" → show as character ev.label || ev.role
  }
});
```

## HTTP API (`looper serve`)

Binds **localhost only** by default (`--host 127.0.0.1`). Optional auth:

* `$LOOPER_SERVE_TOKEN` or `--token` → require `Authorization: Bearer <token>`
* Empty / unset → no auth (fine for a same-machine UI; do not expose to the network)

| Method | Path | Body / notes |
|---|---|---|
| GET | `/v1/version` | Same as `looper version --json` |
| GET | `/v1/doctor?probe=0` | Same as doctor; HTTP 503 when not usable. Optional `project` and `cwd` query params. |
| POST | `/v1/plan` | `{task, loop?, agent?, team?, assign?, role_labels?, meta?, max_iterations?, cwd?}` |
| POST | `/v1/run` | Same; responds with chunked `application/x-ndjson`, header `X-Looper-Run-Id` |
| POST | `/v1/runs/{id}/cancel` | Cancel an in-flight run |
| POST | `/v1/ask` | `{agent, prompt?, system?, tools?, messages?, tool_results?, max_tool_calls?, protocol?, history_trim?, team?, cwd?, project?, role?, approvals?, timeout?, max_tokens?, temperature?}` |
| GET | `/v1/team` | Current team (no secrets) |
| PUT | `/v1/team` | Set in-process team override (same schema as `--team-json`) |
| PUT | `/v1/keys/{provider}` | `{value: "..."}` stores key; returns `{env, source, masked}` only |
| GET | `/v1/keys` | Masked key status |
| GET | `/v1/models/discover` | Local Ollama / LM Studio models |

## `ask` — single character turn (+ optional tool calls)

Frontends that map a persona to a model should call `ask` for each desk turn so
the assigned model actually produces the line:

```bash
looper ask --json --agent sonnet --team-file ./team.json "You are the planner. Reply in one short paragraph: ..."

# Long persona prompts (Office character turns): avoid OS argv limits
printf '%s' "$LONG_PERSONA_PROMPT" | looper ask --json --agent sonnet --stdin
looper ask --json --agent sonnet --prompt-file ./persona.txt
looper ask --json --agent sonnet - < ./persona.txt
```

Capability flags: `ask_stdin`, `ask_tools`, `tool_calls`, `tool_results`, `history_trim` in
`looper version --json`.

For CLI agents, prompts longer than ~100 KB are delivered via the agent's
`prompt_via` setting (`stdin` or `auto`). `prompt_via: arg` (default) raises a
clear error above that threshold — set `prompt_via` to `stdin`/`auto` for CLIs
that read the prompt from stdin.

### Plain text response (no tools)

```json
{
  "ok": true,
  "text": "...",
  "tool_calls": [],
  "finish_reason": "stop",
  "protocol": "none",
  "messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "agent": {"name": "sonnet", "kind": "api", "provider": "anthropic", "model": "claude-sonnet-5-5", "tier": "frontier"},
  "seconds": 1.2,
  "error": null,
  "usage": null,
  "warnings": [],
  "trim": {"applied": false, "before_tokens": 12, "after_tokens": 12, "budget": null, "steps": []}
}
```

Plain text with an empty `tool_calls` array is always a valid response when no
tools were offered, or the model chose not to call any.

### History trimming (`trim` report)

Opt-in via config `history_trim` or per-request `history_trim` (object / `true` /
`false`). Default **off**. When enabled and history is over
`context_window × budget_fill`, looper trims a **copy** for the provider only.

* Response `messages` is always the **full untrimmed** conversation (plus the new
  assistant turn). The caller owns history and must not rely on looper to persist
  stubs.
* Additive field `trim`:

```json
{
  "applied": true,
  "before_tokens": 74000,
  "after_tokens": 12900,
  "budget": 16000,
  "steps": [
    {"lever": "writes", "tokens": 49200, "stubbed": 18},
    {"lever": "results", "tokens": 42900, "trimmed": 12},
    {"lever": "collapse", "tokens": 12900, "collapsed": true, "dropped": 140}
  ]
}
```

When disabled or under budget: `applied: false`, `steps: []`, and (if a window
is known) `budget` may still be set. Token estimate = total message chars /
`defaults.chars_per_token` (3.5).


### Offering caller-owned tools

**looper never executes caller-owned tools.** It signals them on `tool_calls`;
the frontend (e.g. the-office desk handlers) must approve, sandbox, and run them.
`load_skill` and MCP tools that the approval policy marks automatic are the
exception: looper runs those inside the turn and records the results in
`messages`. They are not included in `tool_calls`.

Request (CLI or `POST /v1/ask`):

```json
{
  "agent": "sonnet",
  "prompt": "Add a rate limiter to src/api.js and run the unit tests.",
  "system": "You are Dwight. Be concise.",
  "tools": [
    {
      "name": "write_file",
      "description": "Write or overwrite a file in the workspace",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string"},
          "content": {"type": "string"}
        },
        "required": ["path", "content"]
      }
    },
    {
      "name": "run_command",
      "description": "Run a shell command in the workspace",
      "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"]
      }
    }
  ],
  "max_tool_calls": 4
}
```

```bash
looper ask --json --agent sonnet --request-json - <<'EOF'
{ ... request object ... }
EOF
```

Example response (matches the owner shape; every call also has `id` + `args`):

```json
{
  "ok": true,
  "text": "Here's the change...",
  "tool_calls": [
    {
      "id": "call_ab12",
      "tool": "write_file",
      "path": "src/auth.js",
      "content": "...",
      "args": {"path": "src/auth.js", "content": "..."}
    },
    {
      "id": "call_cd34",
      "tool": "run_command",
      "command": "npm test",
      "args": {"command": "npm test"}
    }
  ],
  "finish_reason": "tool_calls",
  "protocol": "native",
  "messages": [ "... conversation so far; pass back on the next turn ..." ],
  "warnings": []
}
```

Schema: [`tool_call.schema.json`](./tool_call.schema.json).

### Protocol selection (provider × tier)

| Agent / tier | Protocol | Notes |
|---|---|---|
| tier `tiny` | `none` | Tools refused; warning returned; text only |
| tier `small` | `json_block` | Fenced ` ```looper-tools ` JSON; default max 2 calls |
| medium+ HTTP, style `anthropic` | `native` | Messages API `tools` / `tool_use` / `tool_result` |
| medium+ HTTP, style `openai_responses` | `native` | Responses API `tools` + `function_call` / `function_call_output` |
| medium+ HTTP, style `openai` (xAI, OpenRouter, LM Studio, Gemini compat) | `native` | Chat Completions `tools` / `tool_calls` |
| medium+ HTTP, style `ollama` | `native` | `/api/chat` tools |
| any CLI (`claude`/`codex`/`grok`/`agy`) | `json_block` | No native API; text protocol + best-effort read-only CLI flags |

`json_block` instructs the model to emit:

````
```looper-tools
{"tool_calls":[{"tool":"write_file","path":"a.js","content":"..."}]}
```
````

Invalid / unknown / schema-failing calls go into `warnings` and are **not** returned
in `tool_calls` (so the caller never executes them). If the provider (or a
`json_block`) clearly attempted tools but **every** call failed validation,
`finish_reason` is `invalid_tool_calls` (not `stop`) so multi-step loops do not
treat the turn as a finished plain-text answer. Callers can inspect `warnings`
and optionally send error `tool_results` on a follow-up turn.

### Multi-step loop (caller-driven; looper is stateless)

```js
let messages = null;
let toolResults = null;
for (;;) {
  const req = {
    agent: "sonnet",
    tools,
    ...(messages ? { messages, tool_results: toolResults } : { prompt, system }),
  };
  const res = await ask(req);          // CLI --request-json or POST /v1/ask
  showAsCharacter(res.text);
  messages = res.messages;             // provider-neutral; just send back
  if (res.finish_reason === "invalid_tool_calls") {
    // Provider tried tools; all failed validation — see res.warnings. Break or
    // send synthetic error tool_results and continue, depending on your UX.
    break;
  }
  if (res.finish_reason !== "tool_calls" || !res.tool_calls.length) break;
  toolResults = [];
  for (const tc of res.tool_calls) {
    try {
      const output = await desk.execute(tc.tool, tc.args); // caller owns tools
      toolResults.push({ id: tc.id, tool: tc.tool, ok: true, output });
    } catch (e) {
      toolResults.push({ id: tc.id, tool: tc.tool, ok: false, error: String(e) });
    }
  }
}
```

### MCP approval round-trip (`approval_required`)

Additive on api_version 1. When no MCP server is configured, `ask` never returns
this finish reason and the `pending` field is omitted.

looper runs an MCP tool inside the turn only when the approval policy says
`auto` (by default: annotation `readOnlyHint: true` and `destructiveHint` is not
true). Anything else is **not** executed. The response looks like:

```json
{
  "ok": true,
  "text": "I need to write the note.",
  "tool_calls": [],
  "finish_reason": "approval_required",
  "pending": [
    {
      "id": "call_ab12",
      "tool": "mcp__notes__write_note",
      "server": "notes",
      "name": "write_note",
      "args": {"text": "hello"},
      "reason": "not_read_only",
      "annotations": {"readOnlyHint": false, "destructiveHint": false}
    }
  ],
  "messages": []
}
```

`pending[].reason` is one of `not_read_only`, `destructive`, `require_approval`,
`approve_all`. `pending[].tool` is the name the model saw
(`mcp__<server>__<tool>`). `pending[].name` is the tool on the server.

`messages` already contains the assistant turn (with that call) and the results
of any looper-owned tools that ran in the same batch (`load_skill`, auto MCP).
Do not execute those yourself. They are not in `tool_calls`.

If the model also requested a **caller-owned** tool, `finish_reason` is still
`approval_required` and that tool is in `tool_calls` as it is today. Handle both.

Resume on the next stateless call. Send the `messages` you were given, your
`tool_results` for caller tools, and one approval per pending id:

```json
{
  "agent": "sonnet",
  "messages": [],
  "tool_results": [],
  "approvals": [
    {"id": "call_ab12", "decision": "approve"},
    {"id": "call_cd34", "decision": "deny", "reason": "not now"}
  ]
}
```

`decision` is `approve` or `deny` (`approved`/`denied`, `yes`/`no` are accepted).
looper executes each approval, appends the tool result, and continues the turn.
A deny list entry still refuses the call even if the user approved it.
There is no server-side session: the arguments are read back out of `messages`.

```js
if (res.finish_reason === "approval_required") {
  const toolResults = [];
  for (const tc of res.tool_calls) {          // caller tools only
    toolResults.push(await desk.execute(tc));
  }
  const approvals = res.pending.map((p) => ({
    id: p.id,
    decision: userAllows(p) ? "approve" : "deny",
  }));
  return ask({ agent, messages: res.messages, tool_results: toolResults, approvals });
}
```

Same body on `POST /v1/ask`. `looper ask --request-json -` accepts `approvals`,
`project`, and `role` on the request object.

### CLI self-editing caveat

Agent CLIs (Claude Code, Codex, …) have their **own** built-in tools that can
edit files. When the caller offers tools, looper:

1. Instructs the model (via the json_block system prompt) **not** to edit files
   itself and to emit `looper-tools` blocks instead.
2. Appends best-effort read-only flags (configurable per agent as
   `caller_tools_extra_args` in config):
   * `claude`: `--permission-mode plan --disallowedTools Edit,Write,NotebookEdit`
   * `codex`: `--sandbox read-only --ask-for-approval never`
   * `grok` / `agy`: no verified flags yet — rely on the prompt only

This is best-effort. A CLI may still attempt edits; the caller must treat the
workspace as untrusted until tool_calls are approved.

### `run --json-events` and tools (v0.5)

Multi-stage loops (`looper run`) remain **text-only** in v0.5. Pass tools through
`ask` for character/desk turns. A future release may add per-stage
`tool_calls_requested` events; until then, do not pass `--tools-json` to `run`.

For multi-stage work (TDD, explore→plan→code→commit, …) use `run --json-events`
and map `stage_started.role` / `.label` onto personas; use `ask` + tools when a
persona turn must drive desk actions.

## Environment

| Var | Meaning |
|---|---|
| `LOOPER_CONFIG` | Config file path |
| `LOOPER_HOME` | Config/credentials directory (default `~/.config/looper`) |
| `LOOPER_SERVE_TOKEN` | Bearer token for `looper serve` |
| `LOOPER_<AGENT>_BIN` | Override a CLI binary path |
| Provider key envs (`ANTHROPIC_API_KEY`, …) | Read by runners; never logged |

## Security notes

* Never put API keys in argv, team JSON, meta, or role labels.
* `doctor`, `keys list`, and HTTP key endpoints return **masked** values only.
* `looper serve` defaults to `127.0.0.1`. If you bind `0.0.0.0`, set a token.
* Transcripts under `.looper/runs/` may contain task text and model output — treat as sensitive.
* Cancellation kills child CLI processes looper started; it does not revoke provider API calls already in flight beyond closing the HTTP client.
* **Tool calls are untrusted suggestions.** The caller must sandbox, approve, and
  execute them. Never auto-run `run_command` / file writes from a model without a
  policy. looper does not execute caller tools.

## api_version policy

* **1** (current): events and endpoints above.
* Frontends must check `api_version` and refuse newer major versions they do not implement.
* Additive optional fields may appear without a version bump; required field renames/removals bump the version.
