"""Executor: run a loop's stage sequence against real CLIs.

Flow per loop: ``pre`` stages once -> ``iterate.body`` repeated until an exit
signal shows up in a check stage or ``max_iterations`` -> ``post`` stages once.
Global caps (``max_total_calls``, ``max_wall_seconds``) are enforced on every
call. Stage types:

* ``single``   one call; falls back to the next installed agent on error
* ``parallel`` Best-of-N: ``n`` candidates at once, rotating across agents
* ``map``      Orchestrator->Workers: split ``source`` output into items, one call per item

Before anything runs, ``looper.assign.adapt`` fits the loop to the team: which
agent takes each role, context/output limits per agent tier, and any graceful
degradation (skipped/merged stages, smaller Best-of-N, sequential fan-out).
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from looper.assign import (Adaptation, Member, StagePlan, adapt, adapt_prompt, allows, build_members,
                           context_budget, default_available, max_tokens_for, order_members)
from looper.events import CancelToken, Cancelled, EventEmitter
from looper.policy import LoopSpec, Policy, StageSpec, find_signal
from looper.router import RouteDecision
from looper.runners import (CallResult, DispatchRunner, Invocation, PromptTooLongError, Runner,
                           SubprocessRunner, describe_call, prepare_invocation)

EventFn = Callable[[str, dict[str, Any]], None]


class CapExceeded(RuntimeError):
    pass


class Budget:
    """Thread-safe call/wall-clock budget."""

    def __init__(self, max_calls: int, max_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.max_calls = max_calls
        self.max_seconds = max_seconds
        self.clock = clock
        self.start = clock()
        self.calls = 0
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return self.clock() - self.start

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def take(self) -> None:
        with self._lock:
            if self.calls >= self.max_calls:
                raise CapExceeded(f"max_total_calls={self.max_calls} reached")
            if self.elapsed() >= self.max_seconds:
                raise CapExceeded(f"max_wall_seconds={self.max_seconds:g} reached")
            self.calls += 1


from looper.textutil import parse_items, render

# --------------------------------------------------------------------------- records

@dataclass
class StageRecord:
    stage: str
    iteration: int
    type: str
    ok: bool
    output: str
    calls: list[dict[str, Any]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    prompt: str = ""


@dataclass
class RunResult:
    loop: str
    task: str
    status: str  # ok | failed | capped
    stop_reason: str
    iterations: int
    final_output: str
    calls_made: int
    seconds: float
    exit_signal: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    route: dict[str, Any] | None = None
    transcript_path: str | None = None
    team: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- executor

class Executor:
    def __init__(
        self,
        policy: Policy,
        *,
        runner: Runner | None = None,
        available: dict[str, str] | None = None,
        force_agent: str | None = None,
        max_iterations: int | None = None,
        cwd: str | Path | None = None,
        on_event: EventFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        assign: dict[str, str] | None = None,
        cancel: CancelToken | None = None,
        emitter: EventEmitter | None = None,
        meta: dict[str, Any] | None = None,
        role_labels: dict[str, str] | None = None,
        max_output_chars: int = 100_000,
        project: str | Path | None = None,
    ):
        self.policy = policy
        self.cancel = cancel or CancelToken()
        self.runner: Runner = runner or DispatchRunner(cli=SubprocessRunner(cancel=self.cancel))
        self.unavailable: dict[str, str] = {}
        if available is None:
            available, self.unavailable = default_available(policy)
        if force_agent and force_agent not in policy.agents:
            raise ValueError(f"--agent {force_agent}: unknown agent (known: {', '.join(policy.agents)})")
        if force_agent and force_agent not in available:
            from looper.assign import agent_endpoint
            ep, why = agent_endpoint(policy.agents[force_agent])
            if ep is not None:
                available = {**available, force_agent: ep}
        self.available = available
        self.members: dict[str, Member] = {m.name: m for m in build_members(policy, available, force_agent)}
        self._local_locks: dict[str, threading.Semaphore] = {}
        self._locks_guard = threading.Lock()
        self.force_agent = force_agent
        self.max_iterations = max_iterations
        self.cwd = str(cwd) if cwd else None
        self.on_event = on_event or (lambda kind, data: None)
        self.clock = clock
        self.assign = dict(assign or {})
        self.emitter = emitter
        self.meta = dict(meta or {})
        self.role_labels = dict(role_labels or {})
        self.max_output_chars = max_output_chars
        self.project = project
        self._ws = None

    # ---- agent selection
    def preference(self, stage: StageSpec) -> list[str]:
        if self.force_agent:
            return [self.force_agent]
        seen: list[str] = []
        for a in self.policy.stage_preferences(stage) + self.policy.defaults.fallback_agents:
            if a not in seen:
                seen.append(a)
        for a in self.members:  # team members not named in the strategy
            if a not in seen:
                seen.append(a)
        doc = self.workspace().role_doc(stage.role, self.policy.role(stage.role).abstract)
        if doc and doc.model and doc.model in self.policy.agents:
            seen = [doc.model, *[a for a in seen if a != doc.model]]
        return seen

    def workspace(self):
        if self._ws is None:
            from looper.workspace import load_workspace
            start = Path(self.cwd) if self.cwd else Path.cwd()
            self._ws = load_workspace(
                start=start, project=self.project,
                policy_raw=self.policy.raw, policy_path=self.policy.path,
            )
        return self._ws

    def _stage_context(self, stage: StageSpec, agent_name: str | None) -> tuple[str, list[str], dict[str, str]]:
        """Prefix for the stage prompt, plus CLI-native argv/env. Empty when no workspace."""
        from looper.cli_native import native_pass
        ws = self.workspace()
        agent = self.policy.agents.get(agent_name) if agent_name else None
        native = native_pass(agent, ws) if agent is not None else None
        text = ws.render(
            role=stage.role,
            abstract_role=self.policy.role(stage.role).abstract,
            include_agents=not (native and native.passes_agents),
            include_soul=True,
            include_skills=False,
        )
        return text, list(native.extra_argv) if native else [], dict(native.extra_env) if native else {}

    def candidates(self, stage: StageSpec) -> list[str]:
        """Capable available agents for a stage, best first (no loop context)."""
        members = list(self.members.values())
        if self.force_agent:
            return [m.name for m in members]
        role = self.policy.role(stage.role)
        capable = [m for m in members if allows(m, role)] or members
        return [m.name for m in order_members(capable, role, self.preference(stage))]

    def adapt(self, loop_name: str) -> Adaptation:
        return adapt(self.policy, self.policy.loop(loop_name), list(self.members.values()),
                     force_agent=self.force_agent, max_iterations=self.max_iterations,
                     assign=self.assign or None)

    # ---- plan (dry run)
    def plan(self, task: str, loop_name: str, route: RouteDecision | None = None) -> dict[str, Any]:
        orig = self.policy.loop(loop_name)
        ad = self.adapt(loop_name)
        loop = ad.loop
        pre, body, post = loop.segments()
        max_it = ad.max_iterations

        def stage_plan(s: StageSpec, segment: str) -> dict[str, Any]:
            sp = ad.stages[s.name]
            chosen = sp.agent
            m = self.members.get(chosen) if chosen else None
            ctx = {"task": task, "iteration": 1, "item": "<subtask>", "candidate": 1,
                   "outputs": {x.name: f"<output of {x.name}>" for x in orig.stages if x.name != s.name},
                   "previous": "<previous stage output>", "previous_artifact": "<latest artifact>"}
            per_var = context_budget(m, self.policy)[0] if m else self.policy.defaults.max_context_chars
            prompt = render(s.prompt, ctx, per_var)
            if m:
                prompt = adapt_prompt(prompt, m, sp, self.policy)
            prefix, native_argv, _native_env = self._stage_context(s, chosen or (self.preference(s)[0] if self.preference(s) else None))
            if prefix:
                prompt = prefix + "\n\n" + prompt
            ph = f"<prompt: {len(prompt)} chars>"
            if chosen:
                argv = describe_call(self.policy.agents[chosen], self.available.get(chosen, ""), ph)
            else:  # show what *would* run with the top preference
                first = self.policy.agents[self.preference(s)[0]]
                argv = describe_call(first, first.binary, ph)
            if native_argv:
                if argv and (argv[-1] == ph or str(argv[-1]).startswith("<")):
                    argv = [*argv[:-1], *native_argv, argv[-1]]
                else:
                    argv = [*argv, *native_argv]
            calls = s.n if s.type == "parallel" else (s.max_items if s.type == "map" else 1)
            return {
                "stage": s.name, "segment": segment, "type": s.type, "role": s.role,
                "abstract_role": self.policy.role(s.role).abstract,
                "preference": sp.order or self.preference(s), "installed": sp.order, "agent": chosen,
                "tier": m.tier.name if m else None, "status": sp.status, "merged": sp.merged,
                "notes": sp.notes, "temperatures": sp.temperatures,
                "max_output_tokens": max_tokens_for(m) if m and m.agent.kind != "cli" else None,
                "context_chars": context_budget(m, self.policy)[1] if m else None,
                "fanout": calls, "timeout": self._timeout(s, chosen), "argv": argv,
                "prompt_preview": prompt[:400],
            }

        stages = [stage_plan(s, "pre") for s in pre] + [stage_plan(s, "loop") for s in body] + \
                 [stage_plan(s, "post") for s in post]
        worst = sum(p["fanout"] for p in stages if p["segment"] != "loop") + \
            max_it * sum(p["fanout"] for p in stages if p["segment"] == "loop")
        if self.policy.defaults.fallback_on_error:
            worst_note = "excluding fallback retries"
        else:
            worst_note = "no fallback retries"
        missing = [p["stage"] for p in stages if not p["agent"]]
        return {
            "task": task,
            "loop": loop.name,
            "description": loop.description,
            "route": route.to_dict() if route else None,
            "team": [m.to_dict() for m in self.members.values()],
            "team_size": len(self.members),
            "unavailable_agents": self.unavailable,
            "stages": stages,
            "dropped_stages": [{"stage": p.stage.name, "status": p.status, "merged_into": p.merged_into}
                               for p in ad.dropped()],
            "degradations": ad.degradations,
            "warnings": ad.warnings,
            "iterate": None if not loop.iterate else {
                "body": loop.iterate.body, "max_iterations": max_it,
                "configured_max_iterations": orig.iterate.max_iterations if orig.iterate else None,
                "exit_signals": loop.iterate.exit_signals, "check_stages": loop.iterate.check_stages,
            },
            "caps": asdict(loop.caps),
            "worst_case_calls": worst,
            "worst_case_note": worst_note,
            "force_agent": self.force_agent,
            "available_agents": sorted(self.available),
            "stages_without_agent": missing,
        }

    # ---- run
    def _pub(self, kind: str, **data: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit(kind, **data)

    def _agent_info(self, name: str | None) -> dict[str, Any] | None:
        if not name or name not in self.members:
            return None
        return self.members[name].to_dict()

    def _label(self, role: str, abstract: str | None = None) -> str | None:
        return self.role_labels.get(role) or (self.role_labels.get(abstract) if abstract else None)

    def run(
        self, task: str, loop_name: str, route: RouteDecision | None = None, *, write_transcript: bool = True
    ) -> RunResult:
        ad = self.adapt(loop_name)
        loop = ad.loop
        budget = Budget(loop.caps.max_total_calls, loop.caps.max_wall_seconds, self.clock)
        state = _RunState(task=task, loop=loop, aliases=ad.aliases)
        self._ad = ad
        status, stop_reason, exit_sig, iterations = "ok", "completed", None, 0
        pre, body, post = loop.segments()
        self.on_event("loop_start", {"loop": loop.name, "task": task, "degradations": ad.degradations})
        self._pub("run_started", loop=loop.name, task=task, team=[m.to_dict() for m in self.members.values()],
                  degradations=ad.degradations, assign=self.assign or None)
        if route is not None:
            self._pub("routed", **route.to_dict())
        for d in ad.degradations:
            self._pub("degraded", message=d)
        for w in ad.warnings:
            self._pub("warning", message=w)
        try:
            for s in pre:
                self.cancel.check()
                self._run_stage(s, state, budget, 1)
            if body and loop.iterate:
                max_it = ad.max_iterations
                stop_reason = f"max_iterations={max_it} reached without exit signal"
                for it in range(1, max_it + 1):
                    self.cancel.check()
                    iterations = it
                    self.on_event("iteration", {"iteration": it, "max": max_it})
                    hit = None
                    for s in body:
                        self.cancel.check()
                        rec = self._run_stage(s, state, budget, it)
                        if s.name in loop.iterate.check_stages:
                            hit = find_signal(rec.output, loop.iterate.exit_signals)
                            if hit:
                                break
                    if hit:
                        exit_sig = hit
                        stop_reason = f"exit signal {hit!r} in iteration {it}"
                        self.on_event("exit_signal", {"signal": hit, "iteration": it})
                        break
            for s in post:
                self.cancel.check()
                self._run_stage(s, state, budget, max(iterations, 1))
        except Cancelled as exc:
            status, stop_reason = "cancelled", str(exc)
            self._pub("error", message=str(exc), status="cancelled")
        except CapExceeded as exc:
            status, stop_reason = "capped", f"cap hit: {exc}"
            self.on_event("capped", {"reason": str(exc)})
            self._pub("warning", message=stop_reason)
        except _StageFailed as exc:
            status, stop_reason = "failed", str(exc)
            self._pub("error", message=str(exc), status="failed")

        result = RunResult(
            loop=loop.name, task=task, status=status, stop_reason=stop_reason, iterations=iterations,
            final_output=state.final_output(), calls_made=budget.calls, seconds=round(budget.elapsed(), 3),
            exit_signal=exit_sig, stages=state.records, route=route.to_dict() if route else None,
            team=[m.to_dict() for m in self.members.values()], degradations=ad.degradations,
        )
        if write_transcript:
            result.transcript_path = str(self.write_transcript(result))
        self.on_event("loop_end", {"status": status, "reason": stop_reason})
        self._pub("run_finished", status=status, stop_reason=stop_reason, final_output=result.final_output,
                  transcript_path=result.transcript_path, calls_made=result.calls_made,
                  seconds=result.seconds, exit_signal=exit_sig, iterations=iterations)
        return result

    def write_transcript(self, result: RunResult) -> Path:
        base = Path(self.cwd or ".")
        runs = Path(self.policy.defaults.runs_dir)
        runs = runs if runs.is_absolute() else base / runs
        runs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = runs / f"{stamp}-{result.loop}.json"
        payload = {"looper_version": _version(), "created": datetime.now().astimezone().isoformat(),
                   "config_source": self.policy.source,
                   "config_path": str(self.policy.path) if self.policy.path else None, **result.to_dict()}
        payload["transcript_path"] = str(path)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    # ---- internals
    def _timeout(self, stage: StageSpec, agent: str | None) -> float:
        if stage.timeout_seconds:
            return float(stage.timeout_seconds)
        if agent and self.policy.agents[agent].timeout_seconds:
            return float(self.policy.agents[agent].timeout_seconds)  # type: ignore[arg-type]
        return self.policy.defaults.timeout_seconds

    def _local_lock(self, m: Member) -> threading.Semaphore | None:
        if m.agent.kind != "local":
            return None
        key = m.agent.base_url or m.name
        with self._locks_guard:
            if key not in self._local_locks:
                self._local_locks[key] = threading.Semaphore(self.policy.defaults.max_parallel_local)
            return self._local_locks[key]

    def _call(self, stage: StageSpec, prompt: str, order: list[str], budget: Budget,
              plan: StagePlan | None = None, temperature: float | None = None) -> tuple[CallResult, list[CallResult]]:
        """Call agents in order until one succeeds (if fallback_on_error)."""
        attempts: list[CallResult] = []
        if not order:
            pref = ", ".join(self.preference(stage))
            r = CallResult("-", [], False, "", error=f"no installed agent for stage '{stage.name}' (wanted: {pref})")
            return r, [r]
        for agent_name in order:
            self.cancel.check()
            budget.take()
            agent = self.policy.agents[agent_name]
            m = self.members.get(agent_name)
            final = adapt_prompt(prompt, m, plan, self.policy) if m else prompt
            prefix, native_argv, native_env = self._stage_context(stage, agent_name)
            if prefix:
                final = prefix + "\n\n" + final
            timeout = min(self._timeout(stage, agent_name), max(budget.remaining_seconds(), 1.0))
            endpoint = self.available.get(agent_name, "")
            try:
                inv = prepare_invocation(
                    agent, endpoint, final, timeout, self.cwd,
                    max_tokens=max_tokens_for(m) if m else None, temperature=temperature,
                    num_ctx=m.info.context_tokens if m and agent.kind == "local" else None,
                    disable_thinking=bool(m and m.tier.disable_thinking),
                    extra_argv=native_argv,
                    extra_env=native_env,
                )
            except PromptTooLongError as exc:
                res = CallResult(agent_name, [], False, "", error=str(exc))
                attempts.append(res)
                self.on_event("call_failed", {"stage": stage.name, "agent": agent_name, "error": res.error})
                if not self.policy.defaults.fallback_on_error:
                    break
                continue
            self.on_event("call", {"stage": stage.name, "agent": agent_name})
            lock = self._local_lock(m) if m else None
            if lock:
                with lock:
                    res = self.runner(inv)
            else:
                res = self.runner(inv)
            attempts.append(res)
            if res.ok:
                return res, attempts
            self.on_event("call_failed", {"stage": stage.name, "agent": agent_name, "error": res.error})
            if not self.policy.defaults.fallback_on_error:
                break
        return attempts[-1], attempts

    def _run_stage(self, stage: StageSpec, state: "_RunState", budget: Budget, iteration: int) -> StageRecord:
        self.on_event("stage_start", {"stage": stage.name, "iteration": iteration, "type": stage.type})
        t0 = self.clock()
        sp = self._ad.stages.get(stage.name)
        order = sp.order if sp else self.candidates(stage)
        top = self.members.get(order[0]) if order else None
        role = stage.role
        abstract = self.policy.role(role).abstract
        self._pub("stage_started", stage=stage.name, role=role, abstract_role=abstract,
                  agent=self._agent_info(order[0] if order else None),
                  label=self._label(role, abstract), iteration=iteration, type=stage.type)
        limit = context_budget(top, self.policy)[0] if top else self.policy.defaults.max_context_chars
        base_ctx = state.context(iteration)
        rec = StageRecord(stage=stage.name, iteration=iteration, type=stage.type, ok=False, output="")

        if stage.type == "single":
            rec.prompt = render(stage.prompt, base_ctx, limit)
            res, attempts = self._call(stage, rec.prompt, order, budget, sp)
            rec.calls = [_call_dict(a) for a in attempts]
            rec.ok, rec.output, rec.error = res.ok, res.output, res.error
        else:
            if stage.type == "parallel":
                jobs = [(i, None) for i in range(1, stage.n + 1)]
            else:
                items = parse_items(state.outputs.get(stage.source or "", ""), stage.max_items)
                rec.items = items
                jobs = [(i, item) for i, item in enumerate(items, 1)]

            def job(arg: tuple[int, str | None]) -> tuple[int, str | None, CallResult, list[CallResult]]:
                i, item = arg
                rot = order[(i - 1) % len(order):] + order[: (i - 1) % len(order)] if order else []
                ctx = {**base_ctx, "candidate": i, "item": item or ""}
                temps = sp.temperatures if sp else None
                temp = temps[(i - 1) % len(temps)] if temps else None
                try:
                    res, attempts = self._call(stage, render(stage.prompt, ctx, limit), rot, budget, sp, temp)
                except CapExceeded as exc:
                    res = CallResult("-", [], False, "", error=f"cap: {exc}")
                    attempts = [res]
                return i, item, res, attempts

            workers = max(1, min(stage.max_workers if stage.type == "map" else stage.n, len(jobs) or 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(job, jobs))
            parts = []
            for i, item, res, attempts in results:
                rec.calls.extend(_call_dict(a) for a in attempts)
                head = f"### Candidate {i} ({res.agent})" if stage.type == "parallel" else f"### Subtask {i} ({res.agent}): {item}"
                parts.append(f"{head}\n{res.output if res.ok else '[failed] ' + (res.error or '')}")
            ok_count = sum(1 for r in results if r[2].ok)
            rec.ok = ok_count > 0
            rec.output = "\n\n".join(parts)
            if not rec.ok:
                rec.error = "; ".join(r[2].error or "failed" for r in results) or "no items to process"
            if any((r[2].error or "").startswith("cap:") for r in results) and not rec.ok:
                rec.seconds = round(self.clock() - t0, 3)
                state.add(stage, rec)
                raise CapExceeded(next(r[2].error for r in results if (r[2].error or "").startswith("cap:")))

        rec.seconds = round(self.clock() - t0, 3)
        state.add(stage, rec)
        self.on_event("stage_end", {"stage": stage.name, "iteration": iteration, "ok": rec.ok,
                                    "agents": sorted({c['agent'] for c in rec.calls}), "error": rec.error})
        text = rec.output or ""
        truncated = False
        if len(text) > self.max_output_chars:
            text = text[: self.max_output_chars]
            truncated = True
        self._pub("stage_output", stage=stage.name, text=text, truncated=truncated,
                  agent=self._agent_info(order[0] if order else None), iteration=iteration)
        hit = None
        if self._ad.loop.iterate and stage.name in (self._ad.loop.iterate.check_stages or []):
            hit = find_signal(rec.output, self._ad.loop.iterate.exit_signals)
        self._pub("stage_finished", stage=stage.name, status="ok" if rec.ok else "failed",
                  duration=rec.seconds, exit_signal=hit, error=rec.error, iteration=iteration,
                  agent=self._agent_info(order[0] if order else None))
        if not rec.ok:
            if rec.error == "cancelled" or (self.cancel and self.cancel.cancelled):
                raise Cancelled("cancelled")
            raise _StageFailed(f"stage '{stage.name}' failed: {rec.error}")
        return rec


class _StageFailed(RuntimeError):
    pass


@dataclass
class _RunState:
    task: str
    loop: LoopSpec
    outputs: dict[str, str] = field(default_factory=dict)
    records: list[StageRecord] = field(default_factory=list)
    last_artifact: str = ""
    last_stage: StageSpec | None = None
    aliases: dict[str, str] = field(default_factory=dict)  # merged stage -> host stage

    def context(self, iteration: int) -> dict[str, Any]:
        prev = self.records[-1].output if self.records else ""
        return {"task": self.task, "iteration": iteration, "outputs": self.outputs,
                "previous": prev, "previous_artifact": self.last_artifact}

    def add(self, stage: StageSpec, rec: StageRecord) -> None:
        self.records.append(rec)
        if rec.ok:
            self.outputs[stage.name] = rec.output
            for merged, host in self.aliases.items():
                if host == stage.name:
                    self.outputs[merged] = rec.output
            self.last_stage = stage
            if stage.artifact:
                self.last_artifact = rec.output

    def final_output(self) -> str:
        if not self.records:
            return ""
        body = self.loop.iterate.body if self.loop.iterate else []
        st = self.last_stage
        if st and st.name in body and not st.artifact and self.last_artifact:
            return self.last_artifact
        ok = [r for r in self.records if r.ok]
        return ok[-1].output if ok else ""


def _call_dict(c: CallResult) -> dict[str, Any]:
    d = asdict(c)
    if d["argv"]:
        d["argv"] = [a if len(a) < 200 else f"<prompt: {len(a)} chars>" for a in d["argv"]]
    d["seconds"] = round(d["seconds"], 3)
    return d


def _version() -> str:
    from looper import __version__
    return __version__
