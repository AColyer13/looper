"""NDJSON event stream for frontends (``looper run --json-events`` / ``POST /v1/run``).

Public event kinds (api_version 1)::

    run_started, routed, stage_started, stage_output, stage_finished,
    degraded, warning, run_finished, error

Human-readable progress stays on stderr; every public event is one JSON object
per line on the event stream (stdout for the CLI, response body for HTTP),
flushed immediately so a Node ``child_process`` can stream them.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO


API_VERSION = 1

PUBLIC_EVENTS = (
    "run_started",
    "routed",
    "stage_started",
    "stage_output",
    "stage_finished",
    "degraded",
    "warning",
    "run_finished",
    "error",
)


class Cancelled(RuntimeError):
    """Raised when a run is cancelled via SIGTERM/SIGINT or cancel API."""


@dataclass
class CancelToken:
    """Shared cancel flag + registry of killable child processes."""

    _flag: threading.Event = field(default_factory=threading.Event)
    _procs: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        self._flag.set()
        with self._lock:
            for p in list(self._procs):
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        if self._flag.is_set():
            raise Cancelled("cancelled")

    def track(self, proc: Any) -> None:
        with self._lock:
            self._procs.append(proc)

    def untrack(self, proc: Any) -> None:
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass


class EventEmitter:
    """Write flushed NDJSON events. Extra fields (meta, role_labels) are merged in."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        meta: dict[str, Any] | None = None,
        role_labels: dict[str, str] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.stream = stream
        self.meta = dict(meta or {})
        self.role_labels = dict(role_labels or {})
        self.on_event = on_event
        self._lock = threading.Lock()

    def emit(self, kind: str, **data: Any) -> None:
        payload = {"event": kind, **data}
        if self.meta and "meta" not in payload:
            payload["meta"] = self.meta
        if self.role_labels and "role_labels" not in payload:
            # only attach on run_started / stage_* so every line isn't huge; always include when set on stage
            if kind in ("run_started", "routed", "stage_started", "run_finished") or "role" in data:
                payload.setdefault("role_labels", self.role_labels)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            if self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()
            if self.on_event:
                self.on_event(kind, payload)

    def label_for(self, role: str) -> str | None:
        return self.role_labels.get(role) or self.role_labels.get(role.replace("-", "_"))


def install_signal_handlers(token: CancelToken) -> Callable[[], None]:
    """On SIGINT/SIGTERM, cancel the token. Returns a restore function."""
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        token.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            # not in main thread / unsupported
            pass

    def restore() -> None:
        for sig, prev in previous.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError):
                pass

    return restore


def stdout_emitter(*, meta: dict[str, Any] | None = None,
                   role_labels: dict[str, str] | None = None) -> EventEmitter:
    """Line-buffered stdout emitter for ``--json-events``."""
    stream = sys.stdout
    try:
        stream.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return EventEmitter(stream, meta=meta, role_labels=role_labels)
