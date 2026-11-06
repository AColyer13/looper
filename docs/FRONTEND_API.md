# looper Frontend API (api_version 1, package 0.4.1)

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
  "version": "0.4.1",
  "api_version": 1,
  "capabilities": [
    "version", "doctor", "plan", "run_events", "ask", "ask_stdin", "serve",
    "team_override", "assign_override", "role_labels", "meta",
    "keys", "models_discover"
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
| `looper serve --host 127.0.0.1 --port 8765` | Local HTTP API (same shapes) | runs until SIGINT |

Shared flags (plan / run / ask / doctor where relevant):

* `--config PATH` / `$LOOPER_CONFIG`
* `--team-json '{...}'` / `--team-file PATH` — drive the team without editing `~/.config/looper/looper.json`
* `--assign-json '{"planner":"codex","reviewer":"claude"}'` — pin roles to agents (validated; under-tier → warning)
* `--role-labels-json '{"planner":"Michael"}'` — opaque labels echoed in plan/events
* `--meta key=value` (repeatable) / `--meta-json '{...}'` — opaque metadata echoed in events
* `--loop NAME`, `--agent NAME`, `--max-iterations N`, `--cwd DIR`

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
| GET | `/v1/doctor?probe=0` | Same as doctor; HTTP 503 when not usable |
| POST | `/v1/plan` | `{task, loop?, agent?, team?, assign?, role_labels?, meta?, max_iterations?, cwd?}` |
| POST | `/v1/run` | Same; responds with chunked `application/x-ndjson`, header `X-Looper-Run-Id` |
| POST | `/v1/runs/{id}/cancel` | Cancel an in-flight run |
| POST | `/v1/ask` | `{agent, prompt, team?, cwd?, timeout?, max_tokens?, temperature?}` |
| GET | `/v1/team` | Current team (no secrets) |
| PUT | `/v1/team` | Set in-process team override (same schema as `--team-json`) |
| PUT | `/v1/keys/{provider}` | `{value: "..."}` stores key; returns `{env, source, masked}` only |
| GET | `/v1/keys` | Masked key status |
| GET | `/v1/models/discover` | Local Ollama / LM Studio models |

## `ask` — single character turn

Frontends that map a persona to a model should call `ask` for each desk turn so
the assigned model actually produces the line:

```bash
looper ask --json --agent sonnet --team-file ./team.json "You are the planner. Reply in one short paragraph: ..."

# Long persona prompts (Office character turns): avoid OS argv limits
printf '%s' "$LONG_PERSONA_PROMPT" | looper ask --json --agent sonnet --stdin
looper ask --json --agent sonnet --prompt-file ./persona.txt
looper ask --json --agent sonnet - < ./persona.txt
```

Capability flag: `ask_stdin` in `looper version --json`.

For CLI agents, prompts longer than ~100 KB are delivered via the agent's
`prompt_via` setting (`stdin` or `auto`). `prompt_via: arg` (default) raises a
clear error above that threshold — set `prompt_via` to `stdin`/`auto` for CLIs
that read the prompt from stdin.

```json
{
  "ok": true,
  "text": "...",
  "agent": {"name": "sonnet", "kind": "api", "provider": "anthropic", "model": "claude-sonnet-5-5", "tier": "frontier"},
  "seconds": 1.2,
  "error": null,
  "usage": null
}
```

For multi-stage work (TDD, explore→plan→code→commit, …) use `run --json-events`
and map `stage_started.role` / `.label` onto personas.

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

## api_version policy

* **1** (current): events and endpoints above.
* Frontends must check `api_version` and refuse newer major versions they do not implement.
* Additive optional fields may appear without a version bump; required field renames/removals bump the version.
