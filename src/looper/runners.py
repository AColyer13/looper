"""Runners turn (agent, prompt) into a call. Tests inject fakes.

* ``SubprocessRunner`` - kind=cli agents (claude/codex/grok/agy)
* ``looper.http_runner.HttpRunner`` - kind=api/local agents
* ``DispatchRunner`` - picks one of the two per invocation (the default)
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Protocol

from looper.policy import AgentSpec

# Linux ARG_MAX is typically ~2MB total; a single argv element is safer under ~128KB.
# Auto-switch to stdin (when the agent supports it) above this byte size.
PROMPT_ARGV_LIMIT = 100_000


class PromptTooLongError(ValueError):
    """Prompt would exceed the safe argv size and the agent cannot take stdin."""


@dataclass
class CallResult:
    agent: str
    argv: list[str]
    ok: bool
    output: str
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    timed_out: bool = False
    seconds: float = 0.0
    # Tool-calling (ask path). Empty tool_calls + finish_reason "stop" = plain text.
    tool_calls: list = None  # type: ignore[assignment]
    finish_reason: str = "stop"  # stop | tool_calls | length | error
    protocol: str = "none"  # native | json_block | none
    warnings: list = None  # type: ignore[assignment]
    usage: dict | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []
        if self.warnings is None:
            self.warnings = []


@dataclass
class Invocation:
    agent: AgentSpec
    binary_path: str
    prompt: str
    timeout: float
    cwd: str | None = None
    argv: list[str] = field(default_factory=list)
    use_stdin: bool | None = None  # None → decide from agent + prompt size
    # HTTP agents only
    max_tokens: int | None = None
    temperature: float | None = None
    num_ctx: int | None = None
    disable_thinking: bool = False
    # Tool-calling (ask path). Caller owns execution; looper only signals.
    system: str | None = None
    messages: list = field(default_factory=list)  # provider-neutral conversation
    tools: list = field(default_factory=list)  # list[ToolSpec]-like or dicts; http_runner normalizes
    protocol: str = "none"  # native | json_block | none
    max_tool_calls: int | None = None
    extra_argv: list[str] = field(default_factory=list)  # CLI safety flags when caller tools present


def prompt_byte_size(prompt: str) -> int:
    return len(prompt.encode("utf-8"))


def _fill_command(agent: AgentSpec, binary_path: str, *, prompt: str | None) -> list[str]:
    """Fill ``{binary}`` / ``{prompt}``. When ``prompt`` is None, drop pure ``{prompt}``
    parts and strip embedded ``{prompt}`` (stdin delivery)."""
    argv: list[str] = []
    for part in agent.command:
        if part == "{prompt}":
            if prompt is not None:
                argv.append(prompt)
            continue
        filled = part.replace("{binary}", binary_path)
        if prompt is None:
            filled = filled.replace("{prompt}", "")
        else:
            filled = filled.replace("{prompt}", prompt)
        argv.append(filled)
    return argv


def build_argv(agent: AgentSpec, binary_path: str, prompt: str) -> list[str]:
    """Fill ``{binary}`` and ``{prompt}`` placeholders. No shell is involved."""
    return _fill_command(agent, binary_path, prompt=prompt)


def prepare_cli_call(agent: AgentSpec, binary_path: str, prompt: str) -> tuple[list[str], bool]:
    """Return ``(argv, use_stdin)`` for a CLI agent, auto-switching for long prompts.

    * ``prompt_via: stdin`` — always pipe the prompt on stdin (no ``{prompt}`` in argv).
    * ``prompt_via: auto`` — argv for short prompts; stdin above ``PROMPT_ARGV_LIMIT``.
    * ``prompt_via: arg`` — argv; if the prompt is longer than ``PROMPT_ARGV_LIMIT``,
      raise ``PromptTooLongError`` (set ``prompt_via`` to ``stdin`` or ``auto``).
    """
    size = prompt_byte_size(prompt)
    via = agent.prompt_via or "arg"
    long = size > PROMPT_ARGV_LIMIT

    if via == "stdin" or (via == "auto" and long):
        return _fill_command(agent, binary_path, prompt=None), True
    if via == "auto":
        return _fill_command(agent, binary_path, prompt=prompt), False
    if long:
        raise PromptTooLongError(
            f"prompt is {size} bytes (limit {PROMPT_ARGV_LIMIT} for argv delivery); "
            f"set agents.{agent.name}.prompt_via to 'stdin' or 'auto', "
            f"or pass a shorter prompt"
        )
    return _fill_command(agent, binary_path, prompt=prompt), False


class Runner(Protocol):
    def __call__(self, inv: Invocation) -> CallResult: ...


class SubprocessRunner:
    """Runs the real CLI. stdin is closed (or carries the prompt) so nothing blocks on input.

    Pass a ``CancelToken`` so SIGTERM/SIGINT (or an HTTP cancel) can kill the child.
    """

    def __init__(self, cancel=None):
        self.cancel = cancel  # CancelToken | None

    def __call__(self, inv: Invocation) -> CallResult:
        try:
            if inv.use_stdin is not None and inv.argv:
                argv, use_stdin = list(inv.argv), bool(inv.use_stdin)
            else:
                argv, use_stdin = prepare_cli_call(inv.agent, inv.binary_path, inv.prompt)
            if inv.extra_argv:
                # Insert extras before a trailing prompt arg when present.
                if not use_stdin and argv and argv[-1] == inv.prompt:
                    argv = [*argv[:-1], *list(inv.extra_argv), argv[-1]]
                else:
                    argv = [*argv, *list(inv.extra_argv)]
        except PromptTooLongError as exc:
            return CallResult(inv.agent.name, list(inv.argv) if inv.argv else [], False, "",
                              error=str(exc), seconds=0.0)

        env = {**os.environ, **inv.agent.env}
        t0 = time.monotonic()
        if self.cancel is not None:
            try:
                self.cancel.check()
            except Exception as exc:  # Cancelled
                return CallResult(inv.agent.name, argv, False, "", error=str(exc), seconds=0.0)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=inv.cwd,
                env=env,
            )
        except OSError as exc:
            return CallResult(inv.agent.name, argv, False, "", error=str(exc), seconds=time.monotonic() - t0)
        if self.cancel is not None:
            self.cancel.track(proc)
        try:
            try:
                stdout, stderr = proc.communicate(
                    input=inv.prompt if use_stdin else None, timeout=inv.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                out = (stdout or "").strip()
                return CallResult(inv.agent.name, argv, False, out, (stderr or "").strip(),
                                  error=f"timed out after {inv.timeout:g}s",
                                  timed_out=True, seconds=time.monotonic() - t0)
        finally:
            if self.cancel is not None:
                self.cancel.untrack(proc)
        if self.cancel is not None and self.cancel.cancelled:
            return CallResult(inv.agent.name, argv, False, (stdout or "").strip(), (stderr or "").strip(),
                              proc.returncode, error="cancelled", seconds=time.monotonic() - t0)
        ok = proc.returncode == 0
        return CallResult(
            inv.agent.name, argv, ok, (stdout or "").strip(), (stderr or "").strip(), proc.returncode,
            None if ok else f"exit code {proc.returncode}: {(stderr or '').strip()[:300]}",
            seconds=time.monotonic() - t0,
        )


def describe_call(agent: AgentSpec, binary_path: str, prompt: str) -> list[str]:
    """argv for CLIs; a readable pseudo-argv for HTTP agents (no secrets).

    Long prompts that would be delivered via stdin are shown as ``<stdin:N bytes>``
    so plans/transcripts stay small. Prompts that exceed the argv limit without
    stdin support are shown as an error annotation (plan still renders).
    """
    if agent.kind != "cli":
        return [f"{agent.api_style}:{agent.base_url}", f"model={agent.model}", prompt]
    size = prompt_byte_size(prompt)
    try:
        argv, use_stdin = prepare_cli_call(agent, binary_path, prompt)
    except PromptTooLongError as exc:
        return [binary_path, f"<prompt too long: {exc}>"]
    if use_stdin:
        return [*argv, f"<stdin:{size} bytes>"]
    return argv


def prepare_invocation(
    agent: AgentSpec,
    binary_path: str,
    prompt: str,
    timeout: float,
    cwd: str | None = None,
    **http_kw,
) -> Invocation:
    """Build an ``Invocation`` with argv / use_stdin resolved for CLI agents."""
    extra = list(http_kw.pop("extra_argv", None) or [])
    if agent.kind == "cli":
        argv, use_stdin = prepare_cli_call(agent, binary_path, prompt)
        if extra:
            if not use_stdin and argv and argv[-1] == prompt:
                argv = [*argv[:-1], *extra, argv[-1]]
            else:
                argv = [*argv, *extra]
        return Invocation(
            agent, binary_path, prompt, timeout, cwd, argv, use_stdin=use_stdin,
            extra_argv=extra, **http_kw,
        )
    return Invocation(
        agent, binary_path, prompt, timeout, cwd,
        [f"{agent.api_style}:{agent.base_url}", f"model={agent.model}", prompt],
        use_stdin=False, extra_argv=extra, **http_kw,
    )


class DispatchRunner:
    """Route kind=cli to a subprocess runner and kind=api/local to the HTTP runner."""

    def __init__(self, cli: Runner | None = None, http: Runner | None = None):
        self.cli = cli or SubprocessRunner()
        self._http = http

    @property
    def http(self) -> Runner:
        if self._http is None:
            from looper.http_runner import HttpRunner
            self._http = HttpRunner()
        return self._http

    def __call__(self, inv: Invocation) -> CallResult:
        return self.cli(inv) if inv.agent.kind == "cli" else self.http(inv)
