"""Typer CLI.

``looper --loop tdd "add a test"`` works because ``LooperGroup`` rewrites argv:
when the first positional token is not a subcommand, ``run`` is inserted.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional


import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from typer.core import TyperGroup

from looper import __version__
from looper import api as frontend_api
from looper import credentials
from looper.config import ConfigError, bundled_default_text
from looper.detect import detect_agents
from looper.events import CancelToken, install_signal_handlers, stdout_emitter
from looper.executor import Executor
from looper.policy import Policy, load_policy
from looper.router import route as route_prompt

# options (root or `run`) that consume the next token
_VALUE_OPTS = {
    "--loop", "-l", "--config", "-c", "--max-iterations", "-n", "--agent", "-a", "--cwd",
    "--meta", "--meta-json", "--role-labels-json", "--assign-json", "--team-json", "--team-file",
    "--host", "--port", "--token", "--prompt-file", "--timeout", "--request-json", "--tools-json",
}

_RUN_ONLY_FLAGS = {"--dry-run", "--json", "--json-events", "--no-transcript", "--quiet", "-q", "--stdin"}


def _default_to_run(args: list[str], commands: set[str]) -> list[str]:
    """Insert ``run`` unless the first positional token is a known subcommand."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return ["run", *args] if i + 1 < len(args) else args
        if tok.startswith("-") and tok != "-":
            i += 2 if tok in _VALUE_OPTS else 1
            continue
        return args if tok in commands else ["run", *args]
    # no positional at all: options like --loop/--dry-run mean "run with stdin"
    if any(a.split("=")[0] in _VALUE_OPTS or a in _RUN_ONLY_FLAGS for a in args):
        return ["run", *args]
    return args


class LooperGroup(TyperGroup):
    """Default to the ``run`` command when the first positional isn't a subcommand."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        args = _default_to_run(list(args), set(self.commands))
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="looper",
    cls=LooperGroup,
    help="Multi-agent forefront: route a task to a playbook loop and run it across agent CLIs "
    "(claude, codex, grok, agy), hosted APIs and local models.\n\n"
    "First time? Run `looper setup` to pick your models and enter API keys.\n\n"
    "Shorthand: `looper [OPTIONS] \"task\"` == `looper run [OPTIONS] \"task\"`.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
err = Console(stderr=True)
out = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Config file (else $LOOPER_CONFIG, ./looper.json, ~/.config/looper/looper.json, bundled).")
JsonOpt = typer.Option(False, "--json", help="Machine-readable JSON output.")


def _resolve_prompt(
    prompt: Optional[str],
    *,
    use_stdin: bool = False,
    prompt_file: Optional[Path] = None,
    empty_msg: str = "Empty prompt.",
    missing_msg: str = "No prompt given.",
) -> str:
    """Resolve task/prompt from argv, ``-``, ``--stdin``, or ``--prompt-file``.

    Exactly one source must win. ``prompt == "-"`` means read stdin (same as ``--stdin``).
    """
    sources = []
    if prompt_file is not None:
        sources.append("prompt-file")
    if use_stdin or prompt == "-":
        sources.append("stdin")
    if prompt is not None and prompt != "-":
        sources.append("arg")
    if len(sources) > 1:
        err.print("[red]Pass the prompt as an argument, via --stdin / -, or via --prompt-file — not more than one.[/red]")
        raise typer.Exit(2)
    if prompt_file is not None:
        path = prompt_file.expanduser()
        if not path.is_file():
            err.print(f"[red]prompt file not found:[/red] {path}")
            raise typer.Exit(2)
        text = path.read_text(encoding="utf-8")
    elif use_stdin or prompt == "-":
        if sys.stdin.isatty():
            err.print(f"[red]{escape(missing_msg)}[/red] Pipe a prompt on stdin or pass --prompt-file.")
            raise typer.Exit(2)
        text = sys.stdin.read()
    elif prompt is None:
        err.print(f"[red]{escape(missing_msg)}[/red]")
        raise typer.Exit(2)
    else:
        text = prompt
    text = text.strip()
    if not text:
        err.print(f"[red]{escape(empty_msg)}[/red]")
        raise typer.Exit(2)
    return text




def _policy(config: Optional[Path], *, team_json: Optional[str] = None,
            team_file: Optional[Path] = None) -> Policy:
    try:
        pol = load_policy(config)
        if team_file is not None:
            pol = frontend_api.apply_team_override(pol, frontend_api.load_team_file(team_file))
        if team_json:
            data = frontend_api.parse_json_arg(team_json, what="--team-json")
            pol = frontend_api.apply_team_override(pol, data)
        return pol
    except ConfigError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)


def _emit_json(data: Any) -> None:
    out.print_json(json.dumps(data, default=str))


def _version_cb(value: bool) -> None:
    if value:
        out.print(f"looper {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Optional[bool] = typer.Option(None, "--version", "-V", callback=_version_cb, is_eager=True, help="Show version."),
) -> None:
    """looper: policy -> router -> loop executor over multiple agent CLIs."""


# --------------------------------------------------------------------------- version / doctor / plan / ask / serve

@app.command("version")
def version_cmd(json_out: bool = JsonOpt) -> None:
    """Show looper version and frontend api_version (for compatibility checks)."""
    info = frontend_api.version_info()
    if json_out:
        _emit_json(info)
    else:
        out.print(f"looper {info['version']} (api_version={info['api_version']})")


@app.command("doctor")
def doctor_cmd(
    config: Optional[Path] = ConfigOpt,
    json_out: bool = JsonOpt,
    no_probe: bool = typer.Option(False, "--no-probe", help="Skip CLI --version probes."),
    team_json: Optional[str] = typer.Option(None, "--team-json", help="Inline team JSON override."),
    team_file: Optional[Path] = typer.Option(None, "--team-file", help="Team JSON file override."),
) -> None:
    """Check whether looper is usable: config, team, CLIs, keys, local servers."""
    pol = _policy(config, team_json=team_json, team_file=team_file)
    doc = frontend_api.doctor(pol, probe=not no_probe)
    if json_out:
        # print JSON even on failure; exit code signals usability
        sys.stdout.write(json.dumps(doc, default=str) + "\n")
        raise typer.Exit(0 if doc["usable"] else 1)
    colour = "green" if doc["usable"] else "red"
    out.print(f"[{colour}]{'usable' if doc['usable'] else 'not usable'}[/{colour}] · "
              f"config={doc['config']['source']}"
              + (f" ({doc['config']['path']})" if doc['config']['path'] else "")
              + f" · team={doc['team_size']}")
    t = Table(title="Team")
    for col in ("Agent", "Kind", "Tier", "Ready", "Key", "Reason"):
        t.add_column(col)
    for m in doc["team"]:
        key = "-" if m["key_present"] is None else ("yes" if m["key_present"] else "missing")
        t.add_row(m["name"], m["kind"], m["tier"],
                  "[green]yes[/green]" if m["ready"] else "[red]no[/red]", key,
                  escape(m["reason"] or "-"))
    out.print(t)
    for p in doc["problems"]:
        out.print(f"[yellow]•[/yellow] {escape(p)}")
    if not doc["usable"]:
        raise typer.Exit(1)


@app.command("plan")
def plan_cmd(
    prompt: Optional[str] = typer.Argument(None, help="Task to plan. Use - / --stdin / --prompt-file for long tasks."),
    loop: Optional[str] = typer.Option(None, "--loop", "-l", help="Force a loop instead of routing."),
    config: Optional[Path] = ConfigOpt,
    json_out: bool = typer.Option(True, "--json/--human", help="JSON (default) or human plan."),
    agent: Optional[str] = typer.Option(None, "--agent", "-a"),
    max_iterations: Optional[int] = typer.Option(None, "--max-iterations", "-n", min=1),
    team_json: Optional[str] = typer.Option(None, "--team-json"),
    team_file: Optional[Path] = typer.Option(None, "--team-file"),
    assign_json: Optional[str] = typer.Option(None, "--assign-json", help='Pin roles: {"planner":"codex",...}'),
    role_labels_json: Optional[str] = typer.Option(None, "--role-labels-json",
                                                   help='Frontend labels: {"planner":"Michael",...}'),
    meta: Optional[list[str]] = typer.Option(None, "--meta", help="Passthrough key=value (repeatable)."),
    meta_json: Optional[str] = typer.Option(None, "--meta-json", help="Passthrough JSON object."),
    use_stdin: bool = typer.Option(False, "--stdin", help="Read the task from stdin ( avoids OS argv length limits)."),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="Read the task from a file."),
) -> None:
    """Route + adapt a task and print the stage plan (no execution). Default output is JSON."""
    prompt = _resolve_prompt(prompt, use_stdin=use_stdin, prompt_file=prompt_file,
                             empty_msg="Empty task.", missing_msg="No task given.")
    try:
        pol = _policy(config, team_json=team_json, team_file=team_file)
        assign = frontend_api.parse_assign(assign_json)
        labels = frontend_api.parse_role_labels(role_labels_json)
        meta_obj = frontend_api.parse_meta(meta, meta_json)
        warnings = frontend_api.validate_assign(pol, assign) if assign else []
        decision = route_prompt(prompt, pol, override=loop)
        ex = Executor(pol, force_agent=agent, max_iterations=max_iterations, assign=assign or None)
        plan = frontend_api.enrich_plan(ex.plan(prompt, decision.loop, decision),
                                        role_labels=labels, meta=meta_obj, assign=assign,
                                        assign_warnings=warnings)
    except (ConfigError, ValueError) as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if json_out:
        _emit_json(plan)
    else:
        _print_plan(plan)


@app.command("ask")
def ask_cmd(
    prompt: Optional[str] = typer.Argument(
        None, help="Single prompt to send. Use - / --stdin / --prompt-file for long persona prompts."),
    agent: str = typer.Option(..., "--agent", "-a", help="Team agent name (see `looper agents`)."),
    config: Optional[Path] = ConfigOpt,
    json_out: bool = JsonOpt,
    cwd: Optional[Path] = typer.Option(None, "--cwd"),
    team_json: Optional[str] = typer.Option(None, "--team-json"),
    team_file: Optional[Path] = typer.Option(None, "--team-file"),
    timeout: Optional[float] = typer.Option(None, "--timeout"),
    use_stdin: bool = typer.Option(False, "--stdin", help="Read the prompt from stdin (avoids OS argv limits)."),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="Read the prompt from a file."),
    request_json: Optional[str] = typer.Option(
        None, "--request-json",
        help="Full ask request as JSON (or '-' for stdin). Supports tools/messages/tool_results. "
             "When set, prompt argv/--stdin/--prompt-file are optional."),
    tools_json: Optional[str] = typer.Option(
        None, "--tools-json", help="Tools array JSON (merged into the request)."),
) -> None:
    """Send one prompt to one team agent (CLI / API / local). For chat-like character turns.

    With ``--request-json`` / tools, the response may include structured ``tool_calls``
    for the caller to execute (looper never runs them). See docs/FRONTEND_API.md.
    """
    req: dict = {}
    if request_json is not None:
        raw = request_json
        if raw == "-":
            if sys.stdin.isatty():
                err.print("[red]--request-json - requires piped stdin.[/red]")
                raise typer.Exit(2)
            raw = sys.stdin.read()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            err.print(f"[red]--request-json: invalid JSON ({exc})[/red]")
            raise typer.Exit(2)
        if not isinstance(parsed, dict):
            err.print("[red]--request-json must be a JSON object[/red]")
            raise typer.Exit(2)
        req = parsed
        if req.get("agent") and agent and req.get("agent") != agent:
            err.print("[red]--agent conflicts with request.agent[/red]")
            raise typer.Exit(2)
        if not agent and req.get("agent"):
            agent = str(req["agent"])
    if tools_json:
        try:
            tools = json.loads(tools_json)
        except json.JSONDecodeError as exc:
            err.print(f"[red]--tools-json: invalid JSON ({exc})[/red]")
            raise typer.Exit(2)
        req["tools"] = tools

    # Resolve prompt unless the request already carries prompt/messages/tool_results
    has_conversation = bool(req.get("prompt") or req.get("messages") or req.get("tool_results"))
    if prompt is not None or use_stdin or prompt_file is not None:
        if has_conversation and request_json is not None and prompt is None and not use_stdin and prompt_file is None:
            pass
        else:
            resolved = _resolve_prompt(
                prompt, use_stdin=use_stdin, prompt_file=prompt_file,
                empty_msg="Empty prompt.", missing_msg="No prompt given.",
            )
            req["prompt"] = resolved
    elif not has_conversation:
        err.print("[red]No prompt given.[/red] Pass a prompt, --stdin, --prompt-file, or --request-json with prompt/messages.")
        raise typer.Exit(2)

    try:
        pol = _policy(config, team_json=team_json, team_file=team_file)
        result = frontend_api.ask(pol, agent, request=req, cwd=cwd, timeout=timeout)
    except (ConfigError, ValueError) as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if json_out:
        _emit_json(result)
    else:
        if result["ok"]:
            out.print(result["text"], markup=False, highlight=False, soft_wrap=True)
            if result.get("tool_calls"):
                err.print(f"[dim]tool_calls: {len(result['tool_calls'])} "
                          f"(finish_reason={result.get('finish_reason')}) — use --json to see them[/dim]")
        else:
            err.print(f"[red]{escape(result.get('error') or 'failed')}[/red]")
            raise typer.Exit(1)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (default: localhost only)."),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    config: Optional[Path] = ConfigOpt,
    token: Optional[str] = typer.Option(None, "--token",
                                        help="Bearer token (else $LOOPER_SERVE_TOKEN). Empty = no auth."),
) -> None:
    """Run a local HTTP API for frontends (NDJSON streaming on POST /v1/run)."""
    from looper.serve import serve as start_serve
    try:
        httpd = start_serve(host, port, config_path=config, token=token)
    except ConfigError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    err.print(f"[green]looper serve[/green] http://{host}:{port}/v1/version"
              + (" (auth required)" if (token or os.environ.get("LOOPER_SERVE_TOKEN")) else " (no auth)"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        err.print("\nshutting down")
        httpd.shutdown()


# --------------------------------------------------------------------------- agents

@app.command("agents")
def agents_cmd(
    config: Optional[Path] = ConfigOpt,
    json_out: bool = JsonOpt,
    no_probe: bool = typer.Option(False, "--no-probe", help="Skip the `--version` probe."),
) -> None:
    """Show every configured agent: CLIs (installed?), API models (key set?), local models; with tiers."""
    from looper.tiers import resolve_tier
    pol = _policy(config)
    statuses = detect_agents(pol, probe=not no_probe)
    tiers = {n: resolve_tier(pol.agents[n], pol) for n in statuses}
    if json_out:
        _emit_json({n: {**s.to_dict(), "tier": tiers[n].to_dict(), "in_team": bool(pol.team and n in pol.team)}
                    for n, s in statuses.items()})
        return
    t = Table(title="Agents" + (f" · team: {', '.join(pol.team)}" if pol.team else ""))
    for col in ("Agent", "Kind", "Status", "Tier", "Size", "Version / model", "Path / endpoint"):
        t.add_column(col)
    for name, s in statuses.items():
        ok = "[green]installed[/green]" if s.kind == "cli" else "[green]ready[/green]"
        bad = "[red]missing[/red]" if s.kind == "cli" else f"[red]{escape(s.reason)}[/red]"
        team_mark = " ★" if pol.team and name in pol.team else ""
        t.add_row(name + team_mark, s.kind, ok if s.installed else bad, tiers[name].tier, tiers[name].size,
                  escape(s.version or ("?" if s.installed else "-")), escape(s.path or s.binary or "-"))
    out.print(t)
    n = sum(s.installed for s in statuses.values())
    out.print(f"{n}/{len(statuses)} ready · config: {pol.source}{' (' + str(pol.path) + ')' if pol.path else ''}")
    if not pol.team:
        out.print("[dim]no team set: every ready agent may be used. Run `looper setup` to choose a team.[/dim]")


@app.command("models")
def models_cmd(
    config: Optional[Path] = ConfigOpt,
    json_out: bool = JsonOpt,
    discover: bool = typer.Option(False, "--discover", "-d", help="Also list local models from Ollama / LM Studio."),
    ollama_url: str = typer.Option("http://localhost:11434", "--ollama-url"),
    lmstudio_url: str = typer.Option("http://localhost:1234", "--lmstudio-url"),
) -> None:
    """Show the team with tier, params, usable context and allowed roles (and discover local models)."""
    from looper.assign import build_members, default_available
    from looper.local import discover_all
    from looper.tiers import parse_params, resolve_tier, tier_for_size
    pol = _policy(config)
    names = pol.team or list(pol.agents)
    avail, missing = default_available(pol)
    rows = []
    for n in names:
        a = pol.agents[n]
        ti = resolve_tier(a, pol)
        tier = pol.tiers[ti.tier]
        roles = [r for r in tier.roles if not a.roles or r in a.roles]
        rows.append({"name": n, "kind": a.kind, "provider": a.provider, "model": a.model or a.binary,
                     "tier": ti.tier, "tier_source": ti.source, "size": ti.size, "context_tokens": ti.context_tokens,
                     "max_output_tokens": a.max_output_tokens or tier.max_output_tokens, "roles": roles,
                     "tools": a.has_tools, "ready": n in avail, "reason": missing.get(n, "")})
    found = []
    if discover:
        for lm in discover_all(ollama_url=ollama_url, lmstudio_url=lmstudio_url):
            s = parse_params(lm.model)
            tot, act = lm.params_b or s.total_b, lm.active_params_b or s.active_b
            eff = (tot * act) ** 0.5 if tot and act else tot
            found.append({**lm.to_dict(), "tier": tier_for_size(eff, pol).name if eff else None,
                          "spec": f"{lm.provider}:{lm.model}" + (
                              "" if lm.base_url.rstrip("/") in (ollama_url.rstrip("/"), "http://localhost:11434",
                                                                "http://localhost:1234/v1")
                              else f"@{lm.base_url}")})
    if json_out:
        _emit_json({"team": rows, "team_is_explicit": bool(pol.team), "discovered": found,
                    "max_agents": pol.defaults.max_agents, "recommended_agents": pol.defaults.recommended_agents})
        return
    t = Table(title=("Team" if pol.team else "All agents (no team set)") +
              f" · {len(rows)} · sweet spot {pol.defaults.recommended_agents}, max {pol.defaults.max_agents}")
    for col in ("Agent", "Kind", "Model", "Tier", "Size", "Context", "Roles", "Ready"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["name"], r["kind"], escape(str(r["model"])), f"{r['tier']} [dim]({r['tier_source']})[/dim]",
                  r["size"], str(r["context_tokens"] or "-"), ", ".join(r["roles"]) or "any",
                  "[green]yes[/green]" if r["ready"] else f"[red]{escape(r['reason'])}[/red]")
    out.print(t)
    if discover:
        d = Table(title="Discovered local models")
        for col in ("Spec (use with looper setup --agent)", "Params", "Context", "Quant", "Tier"):
            d.add_column(col)
        for f in found:
            d.add_row(escape(f["spec"]), str(f["params_b"] or "?"), str(f["context_window"] or "?"),
                      str(f["quantization"] or "-"), str(f["tier"] or "?"))
        out.print(d if found else "[dim]no local servers answered (Ollama :11434, LM Studio :1234)[/dim]")


# --------------------------------------------------------------------------- loops / show

@app.command("loops")
def loops_cmd(config: Optional[Path] = ConfigOpt, json_out: bool = JsonOpt) -> None:
    """List the loops in the playbook."""
    pol = _policy(config)
    if json_out:
        _emit_json({n: {"description": l.description, "stages": [s.name for s in l.stages]} for n, l in pol.loops.items()})
        return
    t = Table(title=f"Loops ({len(pol.loops)}) · default: {pol.default_loop}")
    t.add_column("Loop", style="bold", no_wrap=True)
    t.add_column("Stages", ratio=2)
    t.add_column("Iterate", no_wrap=True)
    t.add_column("Description", ratio=3)
    for n, l in pol.loops.items():
        it = f"{'+'.join(l.iterate.body)} ×{l.iterate.max_iterations}" if l.iterate else "-"
        t.add_row(n, " → ".join(s.name for s in l.stages), it, l.description)
    out.print(t)


@app.command("show")
def show_cmd(
    loop: str = typer.Argument(..., help="Loop name."),
    config: Optional[Path] = ConfigOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Show a loop's stages, agent preferences, exit conditions, caps and routing rules."""
    pol = _policy(config)
    try:
        l = pol.loop(loop)
    except ConfigError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if json_out:
        _emit_json(pol.raw["loops"][loop])
        return
    out.print(Panel(escape(l.description), title=f"[bold]{loop}[/bold]"))
    t = Table()
    for col in ("#", "Stage", "Type", "Role", "Agent preference"):
        t.add_column(col)
    body = l.iterate.body if l.iterate else []
    for i, s in enumerate(l.stages, 1):
        typ = s.type + (f" n={s.n}" if s.type == "parallel" else f" ← {s.source}" if s.type == "map" else "")
        t.add_row(str(i), s.name + (" ⟳" if s.name in body else ""), typ, s.role, " > ".join(pol.stage_preferences(s)))
    out.print(t)
    if l.iterate:
        out.print(f"⟳ iterate [bold]{' → '.join(l.iterate.body)}[/bold] up to {l.iterate.max_iterations}× "
                  f"until {escape(str(l.iterate.exit_signals))} in {l.iterate.check_stages}")
    out.print(f"caps: max_total_calls={l.caps.max_total_calls}, max_wall_seconds={l.caps.max_wall_seconds:g}")
    out.print("route rules: " + escape(", ".join(f"{r.label}+{r.weight:g}" for r in l.route) or "(none)"))


# --------------------------------------------------------------------------- route

@app.command("route")
def route_cmd(
    prompt: str = typer.Argument(..., help="Task to route."),
    config: Optional[Path] = ConfigOpt,
    loop: Optional[str] = typer.Option(None, "--loop", "-l", help="Explicit loop override."),
    json_out: bool = JsonOpt,
    top: int = typer.Option(5, "--top", help="How many ranked loops to show."),
) -> None:
    """Explain which loop the router picks for a task (runs nothing)."""
    pol = _policy(config)
    try:
        d = route_prompt(prompt, pol, override=loop)
    except ConfigError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    if json_out:
        _emit_json(d.to_dict())
        return
    out.print(Panel(f"[bold]{d.loop}[/bold]  (method: {d.method}, score: {d.score:g})\n{escape(d.reason)}",
                    title=f"route: {escape(prompt)}"))
    t = Table(title="Ranking")
    t.add_column("Loop")
    t.add_column("Score", justify="right")
    t.add_column("Matched rules")
    shown = [r for r in d.ranking if r.score > 0][:top]
    if not shown:
        out.print("[dim]no route rules matched[/dim]")
        return
    for r in shown:
        t.add_row(r.loop, f"{r.score:g}", escape(", ".join(r.matched) or "-"))
    out.print(t)


# --------------------------------------------------------------------------- run

def _print_plan(plan: dict[str, Any]) -> None:
    r = plan.get("route") or {}
    out.print(Panel(
        f"loop: [bold]{plan['loop']}[/bold]  ({r.get('method', '?')}: {escape(r.get('reason', ''))})\n"
        f"{escape(plan['description'])}",
        title="[yellow]DRY RUN[/yellow] plan",
    ))
    team = plan.get("team") or []
    if team:
        out.print("team: " + ", ".join(f"{m['name']} [dim]({m['kind']}, {m['tier']}, {m['size']})[/dim]"
                                       for m in team))
    t = Table()
    t.add_column("Seg", no_wrap=True)
    t.add_column("Stage", no_wrap=True)
    t.add_column("Type", no_wrap=True)
    t.add_column("Role", no_wrap=True)
    t.add_column("Agent → fallbacks")
    t.add_column("Tier", no_wrap=True)
    t.add_column("Notes")
    for s in plan["stages"]:
        agent = f"[green]{s['agent']}[/green]" if s["agent"] else "[red](none installed)[/red]"
        if s.get("status") == "under_tier":
            agent = f"[yellow]{s['agent']}*[/yellow]"
        others = [a for a in s["preference"] if a != s["agent"]]
        if others:
            agent += f" [dim]→ {', '.join(others[:2])}{'…' if len(others) > 2 else ''}[/dim]"
        typ = s["type"] + (f" ×{s['fanout']}" if s["fanout"] > 1 else "")
        name = s["stage"] + (f" [dim]+{'+'.join(s['merged'])}[/dim]" if s.get("merged") else "")
        t.add_row(s["segment"], name, typ, s.get("abstract_role", s["role"]), agent, s.get("tier") or "-",
                  escape("; ".join(s.get("notes") or [])) or "-")
    out.print(t)
    it = plan.get("iterate")
    if it:
        cfg = it.get("configured_max_iterations")
        extra = f" (configured {cfg})" if cfg and cfg != it["max_iterations"] else ""
        out.print(f"⟳ loop {' → '.join(it['body'])} up to {it['max_iterations']}×{extra} until "
                  f"{escape(str(it['exit_signals']))} (checked in {', '.join(it['check_stages'])})")
    c = plan["caps"]
    out.print(f"caps: {c['max_total_calls']} calls / {c['max_wall_seconds']:g}s · worst case ≈ "
              f"{plan['worst_case_calls']} calls ({plan['worst_case_note']})")
    out.print(f"available agents: {', '.join(plan['available_agents']) or '(none)'}")
    for s in plan["stages"]:
        if s["argv"]:
            head = s["argv"][0]
            head = Path(head).name if "/" in head and "://" not in head else head
            out.print(f"[dim]  {s['stage']}: {escape(' '.join([head, *s['argv'][1:]]))}[/dim]")
    for name, why in (plan.get("unavailable_agents") or {}).items():
        out.print(f"[yellow]unavailable:[/yellow] {name}: {escape(why)}")
    if plan.get("degradations"):
        out.print("[bold]adapted to this team:[/bold]")
        for d in plan["degradations"]:
            out.print(f"  • {escape(d)}")
    for w in plan.get("warnings") or []:
        out.print(f"[yellow]note:[/yellow] {escape(w)}")
    if plan["stages_without_agent"]:
        out.print(f"[yellow]warning:[/yellow] no installed agent for: {', '.join(plan['stages_without_agent'])}; "
                  "a real run would fail there. Run `looper setup`, install a CLI, or pass --agent.")


def _event_printer(kind: str, data: dict[str, Any]) -> None:
    if kind == "iteration":
        err.print(f"[cyan]⟳ iteration {data['iteration']}/{data['max']}[/cyan]")
    elif kind == "stage_start":
        err.print(f"[bold]▶ {data['stage']}[/bold] [dim]({data['type']})[/dim]")
    elif kind == "call":
        err.print(f"  [dim]→ {data['agent']}[/dim]")
    elif kind == "call_failed":
        err.print(f"  [yellow]✗ {data['agent']}: {escape(str(data['error']))[:200]}[/yellow]")
    elif kind == "exit_signal":
        err.print(f"[green]✓ exit signal {escape(data['signal'])!s}[/green]")
    elif kind == "capped":
        err.print(f"[red]■ {escape(data['reason'])}[/red]")


@app.command("run")
def run_cmd(
    prompt: Optional[str] = typer.Argument(None, help="Task. Omit, pass -, or use --stdin / --prompt-file."),
    loop: Optional[str] = typer.Option(None, "--loop", "-l", help="Force a loop instead of routing."),
    config: Optional[Path] = ConfigOpt,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; execute nothing."),
    json_out: bool = JsonOpt,
    json_events: bool = typer.Option(False, "--json-events",
                                     help="Stream NDJSON events on stdout (human logs on stderr)."),
    max_iterations: Optional[int] = typer.Option(None, "--max-iterations", "-n", min=1,
                                                 help="Override the loop's max_iterations."),
    agent: Optional[str] = typer.Option(None, "--agent", "-a",
                                        help="Force every stage onto one agent (see `looper agents`)."),
    cwd: Optional[Path] = typer.Option(None, "--cwd",
                                       help="Working directory for the agent CLIs (and .looper/runs)."),
    no_transcript: bool = typer.Option(False, "--no-transcript", help="Don't write .looper/runs/<ts>.json."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print the final output."),
    team_json: Optional[str] = typer.Option(None, "--team-json", help="Inline team JSON override."),
    team_file: Optional[Path] = typer.Option(None, "--team-file", help="Team JSON file override."),
    assign_json: Optional[str] = typer.Option(None, "--assign-json",
                                              help='Pin roles to agents: {"planner":"codex",...}'),
    role_labels_json: Optional[str] = typer.Option(
        None, "--role-labels-json", help='Frontend labels echoed in events: {"planner":"Michael",...}'),
    meta: Optional[list[str]] = typer.Option(None, "--meta", help="Passthrough key=value (repeatable)."),
    meta_json: Optional[str] = typer.Option(None, "--meta-json", help="Passthrough JSON object."),
    use_stdin: bool = typer.Option(False, "--stdin", help="Read the task from stdin (avoids OS argv limits)."),
    prompt_file: Optional[Path] = typer.Option(None, "--prompt-file", help="Read the task from a file."),
) -> None:
    """Route the task, then run the chosen loop with real CLIs / models."""
    # Backward compatible: omitting the task on a pipe still reads stdin.
    if prompt is None and not use_stdin and prompt_file is None and not sys.stdin.isatty():
        use_stdin = True
    prompt = _resolve_prompt(prompt, use_stdin=use_stdin, prompt_file=prompt_file,
                             empty_msg="Empty task.", missing_msg="No task given. Usage: looper run \"task\"")
    if json_out and json_events:
        err.print("[red]Use either --json or --json-events, not both.[/red]")
        raise typer.Exit(2)

    try:
        pol = _policy(config, team_json=team_json, team_file=team_file)
        assign = frontend_api.parse_assign(assign_json)
        labels = frontend_api.parse_role_labels(role_labels_json)
        meta_obj = frontend_api.parse_meta(meta, meta_json)
        assign_warnings = frontend_api.validate_assign(pol, assign) if assign else []
        decision = route_prompt(prompt, pol, override=loop)
        cancel = CancelToken()
        emitter = stdout_emitter(meta=meta_obj, role_labels=labels) if json_events else None
        human_events = None if (quiet or json_out or json_events) else _event_printer
        ex = Executor(pol, force_agent=agent, max_iterations=max_iterations, cwd=cwd,
                      on_event=human_events, assign=assign or None, cancel=cancel,
                      emitter=emitter, meta=meta_obj, role_labels=labels)
    except (ConfigError, ValueError) as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)

    if dry_run:
        plan = frontend_api.enrich_plan(ex.plan(prompt, decision.loop, decision),
                                        role_labels=labels, meta=meta_obj, assign=assign,
                                        assign_warnings=assign_warnings)
        _emit_json(plan) if (json_out or json_events) else _print_plan(plan)
        return

    restore = install_signal_handlers(cancel) if json_events else (lambda: None)
    if not (quiet or json_out or json_events):
        err.print(f"[bold]loop:[/bold] {decision.loop} [dim]({escape(decision.reason)})[/dim]")
        for w in assign_warnings:
            err.print(f"[yellow]note:[/yellow] {escape(w)}")
    try:
        result = ex.run(prompt, decision.loop, decision, write_transcript=not no_transcript)
    finally:
        restore()

    if json_events:
        # final event already emitted; exit code only
        raise typer.Exit({"ok": 0, "capped": 0, "cancelled": 130}.get(result.status, 1))
    if json_out:
        _emit_json(result.to_dict())
    else:
        if result.final_output:
            out.print(result.final_output, markup=False, highlight=False, soft_wrap=True)
        if not quiet:
            colour = {"ok": "green", "capped": "yellow", "cancelled": "yellow"}.get(result.status, "red")
            err.print(f"[{colour}]{result.status}[/{colour}]: {escape(result.stop_reason)} · "
                      f"{result.calls_made} calls · {result.seconds:.1f}s"
                      + (f" · transcript: {result.transcript_path}" if result.transcript_path else ""))
    if result.status == "failed":
        raise typer.Exit(1)
    if result.status == "cancelled":
        raise typer.Exit(130)


# --------------------------------------------------------------------------- setup / keys / spec

def _prompt_keys(needed: dict[str, list[str]], *, use_keyring: bool, interactive: bool) -> dict[str, str]:
    """Step 2 of setup: one hidden prompt per provider key. Returns env -> where it is now."""
    result: dict[str, str] = {}
    for env, provs in needed.items():
        src = credentials.key_source(env)
        label = "/".join(provs)
        if src != "missing":
            if not interactive or typer.confirm(f"{label}: ${env} already set ({src}, "
                                                f"{credentials.mask(credentials.get_key(env))}). Keep it?",
                                                default=True):
                result[env] = src
                continue
        if not interactive:
            result[env] = "missing"
            continue
        val = typer.prompt(f"{label} API key for ${env} (input hidden; leave blank to skip)",
                           default="", show_default=False, hide_input=True)
        if not val.strip():
            result[env] = "skipped"
            continue
        where = credentials.set_key(env, val, use_keyring=use_keyring)
        result[env] = "keyring" if where == "keyring" else "file"
    return result


@app.command("setup")
def setup_cmd(
    agent: Optional[list[str]] = typer.Option(
        None, "--agent", "-a",
        help="Non-interactive: provider:model (repeatable, in team order). e.g. anthropic:claude-sonnet-5-5, "
             "cli:codex, ollama:qwen3.5:9b, openai_compatible:my-model@http://localhost:8000/v1, add #tier to override."),
    path: Optional[Path] = typer.Option(None, "--path", "-p", help="Config file to write (default: user config)."),
    config: Optional[Path] = ConfigOpt,
    prompt_keys: bool = typer.Option(False, "--prompt-keys", help="With --agent: also prompt (hidden) for missing keys."),
    use_keyring: bool = typer.Option(False, "--keyring", help="Store keys in the OS keyring (needs `keyring`)."),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Query local servers for params/context."),
    json_out: bool = JsonOpt,
) -> None:
    """Pick your models (1-5 agents), then enter each API key. Writes the team to looper.json; keys go to
    credentials.env (0600) or the OS keyring, never into looper.json."""
    from looper import setup as su
    pol = _policy(config)
    target = path or su.default_config_path()
    interactive = not agent
    if use_keyring and not credentials.keyring_available():
        err.print("[red]--keyring: the `keyring` package isn't installed/usable[/red] (pip install 'looper[keyring]')")
        raise typer.Exit(2)

    choices: list[su.Choice] = []
    if interactive:
        from looper.local import discover_all
        statuses = detect_agents(pol, probe=False, names=[n for n, a in pol.agents.items() if a.kind == "cli"])
        local_models = discover_all() if probe else []
        opts = su.menu(pol, {n: s.installed for n, s in statuses.items()}, local_models)
        out.print(Panel(
            "Step 1/2 - choose your models. Step 2/2 - enter API keys for the providers you picked.\n"
            f"Sweet spot: {pol.defaults.recommended_agents} agents (a strong planner/judge, a coder, a reviewer "
            f"from a different vendor). Max: {pol.defaults.max_agents}.\n"
            "Local models run one at a time on your GPU; small models get simpler roles automatically.",
            title="looper setup"))
        t = Table(show_header=True)
        t.add_column("#", justify="right")
        t.add_column("Spec")
        t.add_column("What")
        for i, (spec, label) in enumerate(opts, 1):
            t.add_row(str(i), escape(spec), escape(label))
        out.print(t)
        out.print("[dim]Or type any provider:model, e.g. openrouter:qwen/qwen3.6-27b or "
                  "openai_compatible:my-model@http://localhost:8000/v1[/dim]")
        n = typer.prompt(f"How many agents? (1-{pol.defaults.max_agents})",
                         default=min(pol.defaults.recommended_agents, max(1, len(opts))), type=int)
        try:
            for w in su.validate_team_size(n, pol):
                out.print(f"[yellow]note:[/yellow] {w}")
        except ValueError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(2)
        while len(choices) < n:
            ans = typer.prompt(f"Agent {len(choices) + 1}/{n} (number or provider:model)").strip()
            spec = opts[int(ans) - 1][0] if ans.isdigit() and 1 <= int(ans) <= len(opts) else ans
            try:
                c = su.parse_agent_spec(spec, pol)
            except (ValueError, IndexError) as exc:
                err.print(f"[red]{escape(str(exc))}[/red]")
                continue
            if any(x.spec == c.spec for x in choices):
                err.print("[yellow]already picked[/yellow]")
                continue
            choices.append(su.enrich_local(c, pol) if probe else c)
    else:
        try:
            for w in su.validate_team_size(len(agent or []), pol):
                err.print(f"[yellow]note:[/yellow] {w}")
            for spec in agent or []:
                c = su.parse_agent_spec(spec, pol)
                choices.append(su.enrich_local(c, pol) if probe else c)
        except ValueError as exc:
            err.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(2)
    su.unique_names(choices)

    # Step 2: keys, only after every model is chosen
    needed = su.needed_keys(choices, pol)
    if needed and (interactive or prompt_keys):
        out.print(Panel("Step 2/2 - API keys. Input is hidden. Keys are stored in "
                        + ("the OS keyring" if use_keyring else f"{credentials.credentials_path()} (mode 600)")
                        + ", never in looper.json.", title="keys"))
    keys = _prompt_keys(needed, use_keyring=use_keyring, interactive=interactive or prompt_keys)

    try:
        su.write_team(target, choices)
        new_pol = load_policy(target)
    except ConfigError as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    from looper.tiers import resolve_tier
    team = [{"name": c.name, "spec": c.spec, "kind": c.kind, **resolve_tier(new_pol.agents[c.name], new_pol).to_dict()}
            for c in choices]
    summary = {"config_path": str(target), "team": team, "keys": keys,
               "credentials_path": str(credentials.credentials_path())}
    if json_out:
        _emit_json(summary)
        return
    t = Table(title=f"Team written to {target}")
    for col in ("Agent", "Kind", "Tier", "Size", "Context"):
        t.add_column(col)
    for m in team:
        t.add_row(m["name"], m["kind"], f"{m['tier']} [dim]({m['source']})[/dim]", m["size"],
                  str(m["context_tokens"] or "-"))
    out.print(t)
    for env, st in keys.items():
        colour = "green" if st in ("env", "file", "keyring") else "yellow"
        out.print(f"  ${env}: [{colour}]{st}[/{colour}]" +
                  ("  → run `looper keys set " + env + "`" if st in ("missing", "skipped") else ""))
    out.print('Next: `looper models` · `looper run "your task" --dry-run`')


keys_app = typer.Typer(help="Manage API keys (stored outside looper.json; values are never printed).",
                       no_args_is_help=True)
app.add_typer(keys_app, name="keys")


def _env_for(name: str, pol: Policy) -> tuple[str, list[str]]:
    if name in pol.providers:
        env = pol.providers[name].api_key_env
        if not env:
            raise typer.BadParameter(f"provider '{name}' doesn't use an API key")
        return env, [name]
    try:
        credentials.validate_name(name)
    except ValueError:
        raise typer.BadParameter(f"'{name}' is neither a provider ({', '.join(pol.providers)}) nor an ENV_VAR name")
    return name, [p for p, v in pol.providers.items() if v.api_key_env == name]


@keys_app.command("set")
def keys_set(
    name: str = typer.Argument(..., help="Provider (anthropic, openai, xai, google, openrouter, ...) or ENV var name."),
    stdin: bool = typer.Option(False, "--stdin", help="Read the key from stdin instead of a hidden prompt."),
    use_keyring: bool = typer.Option(False, "--keyring", help="Store in the OS keyring."),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Store a key (hidden prompt). Never pass keys as arguments."""
    pol = _policy(config)
    env, provs = _env_for(name, pol)
    val = sys.stdin.readline() if stdin else typer.prompt(f"{env} (hidden)", hide_input=True)
    try:
        where = credentials.set_key(env, val, use_keyring=use_keyring)
    except (ValueError, RuntimeError) as exc:
        err.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2)
    out.print(f"stored ${env} ({credentials.mask(val.strip())}) in {where}")


@keys_app.command("list")
def keys_list(config: Optional[Path] = ConfigOpt, json_out: bool = JsonOpt) -> None:
    """Show which keys are set and where (masked)."""
    pol = _policy(config)
    envs: dict[str, list[str]] = {}
    for p, v in pol.providers.items():
        if v.api_key_env:
            envs.setdefault(v.api_key_env, []).append(p)
    rows = credentials.status(envs)
    if json_out:
        _emit_json([r.__dict__ for r in rows])
        return
    t = Table(title=f"API keys · file: {credentials.credentials_path()}"
              + (" · keyring: on" if credentials.keyring_available() else ""))
    for col in ("Env var", "Providers", "Source", "Value"):
        t.add_column(col)
    for r in rows:
        src = {"missing": "[dim]missing[/dim]"}.get(r.source, f"[green]{r.source}[/green]")
        t.add_row(r.name, ", ".join(r.providers) or "-", src, r.masked)
    out.print(t)


@keys_app.command("remove")
def keys_remove(name: str = typer.Argument(..., help="Provider or ENV var name."),
                config: Optional[Path] = ConfigOpt) -> None:
    """Delete a stored key (file and keyring; your shell env is untouched)."""
    pol = _policy(config)
    env, _ = _env_for(name, pol)
    removed = credentials.remove_key(env)
    out.print(f"removed ${env} from {', '.join(removed)}" if removed else f"${env} was not stored")
    if os.environ.get(env):
        out.print(f"[yellow]note:[/yellow] ${env} is still set in your shell environment")


@app.command("spec")
def spec_cmd(
    config: Optional[Path] = ConfigOpt,
    write: Optional[Path] = typer.Option(None, "--write", "-w", help="Write to a file instead of stdout."),
) -> None:
    """Export the language-neutral team/tier spec (tiers, roles, assignment algorithm) as JSON."""
    from looper.spec import build_spec
    data = build_spec(_policy(config))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if write:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(text, encoding="utf-8")
        out.print(f"wrote {write}")
    else:
        sys.stdout.write(text)


# --------------------------------------------------------------------------- init / validate

@app.command("init")
def init_cmd(
    path: Path = typer.Option(Path("looper.json"), "--path", "-p", help="Where to write the config."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing file."),
) -> None:
    """Write the full sample config (all playbook loops) to ./looper.json."""
    if path.exists() and not force:
        err.print(f"[red]{path} exists[/red] (use --force)")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bundled_default_text(), encoding="utf-8")
    out.print(f"wrote {path.resolve()}")


@app.command("validate")
def validate_cmd(config: Optional[Path] = ConfigOpt) -> None:
    """Validate the effective config."""
    pol = _policy(config)
    out.print(f"[green]ok[/green]: {len(pol.loops)} loops, {len(pol.agents)} agents, source={pol.source}"
              + (f" ({pol.path})" if pol.path else "")
              + (f", team={len(pol.team)}/{pol.defaults.max_agents}" if pol.team else ""))


def main() -> None:  # console_script entry point
    app()


if __name__ == "__main__":
    main()
