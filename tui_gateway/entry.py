import os
import sys

# Stop a ``utils/`` (or ``proxy/``, ``ui/``) package in the launch directory
# from shadowing Hermes's own top-level modules.  ``hermes_bootstrap`` lives at
# the repo root next to this package, so importing it is safe before the guard
# runs (its name won't collide with a user package), and it owns the canonical
# path-hardening logic shared with the other entry points.
import hermes_bootstrap

hermes_bootstrap.harden_import_path()

import json
import logging
import signal
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from hermes_cli.client_auth.runtime import (
    AuthRequired,
    account_login,
    account_logout,
    account_status,
    authorize_entrypoint,
    clear_runtime_consumer,
)
from tui_gateway._stdin_recovery import handle_spurious_eof

logger = logging.getLogger(__name__)

_CRASH_LOG = os.path.join(
    os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"),
    "logs",
    "tui_gateway_crash.log",
)
_AUTH_METHODS = frozenset({"auth.status", "auth.login", "auth.logout"})
_AUTH_PARAMS = {
    "auth.status": frozenset(),
    "auth.login": frozenset({"username", "password"}),
    "auth.logout": frozenset(),
}
_AUTH_REASONS = frozenset(
    {
        "interactive_login_required",
        "invalid_credentials",
        "rate_limited",
        "runtime_unavailable",
        "server_unavailable",
        "session_expired",
        "session_rejected",
        "signed_out",
        "vault_unavailable",
    }
)


class _StatusLike(Protocol):
    state: Any
    runtime_instance_id: str
    epoch: int
    reason: str | None

    def public_dict(self) -> dict[str, object]: ...


class _AuthRuntimeLike(Protocol):
    def status(self) -> _StatusLike: ...

    def login(self, username: str, password: bytearray) -> _StatusLike: ...

    def logout(self) -> _StatusLike: ...

    def require(self, boundary: str, status: _StatusLike) -> None: ...


class _GatewayLike(Protocol):
    def dispatch(self, request: dict) -> dict | None: ...

    def start(self) -> Mapping[str, object] | None: ...

    def after_ready(self) -> None: ...

    def stop(self) -> None: ...


def _state_text(status: _StatusLike) -> str:
    value = getattr(status, "state", "locked")
    return str(getattr(value, "value", value))


def _safe_reason(reason: object) -> str:
    return str(reason) if reason in _AUTH_REASONS else "runtime_unavailable"


def _auth_event(kind: str, status: _StatusLike) -> dict[str, object]:
    payload = status.public_dict()
    if not isinstance(payload, dict):
        raise AuthRequired("runtime_unavailable")
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": kind, "payload": payload},
    }


def _rpc_error(
    request_id: object,
    code: int,
    message: str,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if reason is not None:
        error["data"] = {"reason": _safe_reason(reason)}
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class AccountAuthShell:
    """Auth-only JSON-RPC gate for the stdio Ink gateway.

    The injected gateway is not started until an exact runtime owner scope has
    been authorized. The scope tuple stays process-local and every protected
    request revalidates it before capability dispatch.
    """

    def __init__(
        self,
        auth: _AuthRuntimeLike,
        gateway: _GatewayLike,
        emit: Callable[[dict], object],
    ) -> None:
        self._auth = auth
        self._gateway = gateway
        self._emit = emit
        self._status: _StatusLike | None = None
        self._scope: tuple[str, int] | None = None
        self._gateway_started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        status = self._read_status()
        self._status = status
        self._emit(_auth_event("auth.status", status))
        if _state_text(status) == "authenticated":
            self._authorize(status)
            self._start_gateway()

    def dispatch(self, request: object) -> dict | None:
        with self._lock:
            return self._dispatch_locked(request)

    def _dispatch_locked(self, request: object) -> dict | None:
        normalized = self._normalize_request(request)
        if isinstance(normalized, dict):
            return normalized
        request_id, method, params = normalized

        if method in _AUTH_METHODS:
            return self._dispatch_auth(request_id, method, params)

        try:
            status = self._read_status()
            self._authorize(status)
        except AuthRequired as error:
            self._lock_gateway()
            self._publish_changed(self._status)
            return _rpc_error(
                request_id,
                20,
                "AUTH_REQUIRED",
                reason=error.reason or "runtime_unavailable",
            )

        return self._gateway.dispatch(request)  # type: ignore[arg-type]

    def poll(self) -> None:
        """Revalidate an active owner even while the TUI is idle."""
        with self._lock:
            if self._scope is None:
                return
            try:
                status = self._read_status()
                observed_scope = (status.runtime_instance_id, status.epoch)
                if (
                    _state_text(status) != "authenticated"
                    or observed_scope != self._scope
                ):
                    raise AuthRequired(
                        getattr(status, "reason", None) or "session_rejected"
                    )
                self._auth.require("tui.agent", status)
            except AuthRequired as error:
                status = self._status or _UnavailableStatus(error.reason)
                self._lock_gateway()
                self._publish_changed(status)
            except Exception:
                status = _UnavailableStatus("runtime_unavailable")
                self._status = status
                self._lock_gateway()
                self._publish_changed(status)

    def _normalize_request(
        self, request: object
    ) -> tuple[object, str, dict[str, object]] | dict[str, object]:
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "invalid request")
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
            return _rpc_error(request_id, -32600, "invalid request")
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "invalid params")
        return request_id, method, params

    def _dispatch_auth(
        self,
        request_id: object,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        if set(params) != _AUTH_PARAMS[method]:
            return _rpc_error(request_id, -32602, "invalid params")
        try:
            if method == "auth.login":
                status = self._login(params)
            elif method == "auth.logout":
                self._lock_gateway()
                status = self._auth.logout()
            else:
                status = self._read_status()
        except AuthRequired as error:
            self._lock_gateway()
            return _rpc_error(
                request_id,
                20,
                "AUTH_REQUIRED",
                reason=error.reason or "runtime_unavailable",
            )
        except (TypeError, ValueError):
            return _rpc_error(request_id, -32602, "invalid params")

        self._status = status
        if _state_text(status) == "authenticated":
            try:
                self._authorize(status)
            except AuthRequired as error:
                self._lock_gateway()
                return _rpc_error(
                    request_id,
                    20,
                    "AUTH_REQUIRED",
                    reason=error.reason or "runtime_unavailable",
                )
            if method == "auth.login":
                self._publish_changed(status)
            self._start_gateway()
        else:
            self._lock_gateway()
            if method == "auth.logout":
                self._publish_changed(status)

        return {"jsonrpc": "2.0", "id": request_id, "result": status.public_dict()}

    def _login(self, params: dict[str, object]) -> _StatusLike:
        username = params.get("username")
        password_text = params.get("password")
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 150
            or not isinstance(password_text, str)
            or not password_text
            or len(password_text) > 4096
        ):
            raise ValueError("invalid login fields")
        password = bytearray(password_text.encode("utf-8"))
        password_text = ""
        try:
            return self._auth.login(username.strip(), password)
        finally:
            password[:] = b"\0" * len(password)

    def _read_status(self) -> _StatusLike:
        try:
            status = self._auth.status()
        except AuthRequired as error:
            self._status = _UnavailableStatus(error.reason)
            raise
        self._status = status
        return status

    def _authorize(self, status: _StatusLike) -> None:
        if _state_text(status) != "authenticated":
            raise AuthRequired(_safe_reason(getattr(status, "reason", None)))
        scope = (status.runtime_instance_id, status.epoch)
        if self._scope is not None and self._scope != scope:
            self._lock_gateway()
        self._auth.require("tui.agent", status)
        self._scope = scope

    def _start_gateway(self) -> None:
        if self._gateway_started:
            return
        ready_payload = dict(self._gateway.start() or {})
        self._gateway_started = True
        self._emit(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "gateway.ready", "payload": ready_payload},
            }
        )
        after_ready = getattr(self._gateway, "after_ready", None)
        if callable(after_ready):
            after_ready()

    def _lock_gateway(self) -> None:
        self._scope = None
        if self._gateway_started:
            self._gateway_started = False
            self._gateway.stop()

    def _publish_changed(self, status: _StatusLike | None) -> None:
        if status is not None:
            self._emit(_auth_event("auth.changed", status))


class _UnavailableStatus:
    def __init__(self, reason: str | None = None) -> None:
        self.state = "locked"
        self.username = None
        self.runtime_instance_id = "unavailable"
        self.epoch = 0
        self.valid_until = 0.0
        self.session_expires_at = None
        self.reason = _safe_reason(reason)

    def public_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "username": self.username,
            "runtime_instance_id": self.runtime_instance_id,
            "epoch": self.epoch,
            "valid_until": self.valid_until,
            "session_expires_at": self.session_expires_at,
            "reason": self.reason,
        }


class _RuntimeAuth:
    def status(self):
        return account_status()

    def login(self, username: str, password: bytearray):
        return account_login(username, password)

    def logout(self):
        clear_runtime_consumer()
        return account_logout()

    def require(self, boundary: str, status: _StatusLike) -> None:
        scope = authorize_entrypoint(boundary, interactive=False)
        if (
            scope.runtime_instance_id != status.runtime_instance_id
            or scope.epoch != status.epoch
        ):
            clear_runtime_consumer()
            raise AuthRequired("runtime_unavailable")


class _ServerGateway:
    def __init__(self) -> None:
        self._server = None
        self._sidecar_installed = False

    def _load(self):
        if self._server is None:
            from tui_gateway import server

            self._server = server
        return self._server

    def start(self) -> Mapping[str, object]:
        server = self._load()
        if not self._sidecar_installed:
            _install_sidecar_publisher(server)
            self._sidecar_installed = True
        ensure_mcp_discovery_started()
        server._ensure_skin_watcher()
        return {"skin": server.resolve_skin(), "change_events": True}

    def after_ready(self) -> None:
        try:
            from hermes_cli.model_switch import prewarm_picker_cache_async

            prewarm_picker_cache_async()
        except Exception:
            logger.debug("picker cache prewarm (tui) failed to start", exc_info=True)

    def dispatch(self, request: dict) -> dict | None:
        return self._load().dispatch(request)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server._shutdown_sessions()


# Handle for the background MCP tool-discovery thread (see
# ensure_mcp_discovery_started).  The first agent build briefly joins this so
# already-spawning fast servers land before the agent snapshots its tool list
# (see wait_for_mcp_discovery).  Stays None when discovery is delegated to the
# shared owner in hermes_cli.mcp_startup — the wait/in-flight/join helpers
# below consult both owners.
_mcp_discovery_thread = None

# True once ensure_mcp_discovery_started decided this process has MCP servers
# configured and spawned discovery through the shared owner. Lets
# wait_for_mcp_discovery re-invoke the (idempotent) spawn on later agent
# builds so the retry-after-zero-connected allowance in
# hermes_cli.mcp_startup.start_background_mcp_discovery can actually fire —
# without this, the single spawn is the only call and a first run that
# connected nothing latches the process MCP-less. Kept as a flag (rather than
# re-probing config) so non-MCP sessions never pay the tools.mcp_tool import
# on the per-agent-build wait path.
_mcp_discovery_enabled = False


def _install_sidecar_publisher(server_module) -> None:
    """Mirror every dispatcher emit to the dashboard sidebar via WS.

    Activated by `HERMES_TUI_SIDECAR_URL`, set by the dashboard's
    ``/api/pty`` endpoint when a chat tab passes a ``channel`` query param.
    Best-effort: connect failure or runtime drop falls back to stdio-only.
    """
    url = os.environ.get("HERMES_TUI_SIDECAR_URL")

    if not url:
        return

    from tui_gateway.event_publisher import WsPublisherTransport
    from tui_gateway.transport import TeeTransport

    server_module._stdio_transport = TeeTransport(
        server_module._stdio_transport, WsPublisherTransport(url)
    )


# How long to wait for orderly shutdown (atexit + finalisers) before
# falling back to ``os._exit(0)`` so a wedged worker mid-flush can't
# strand the process.  1s covers the gateway's own shutdown work
# (thread-pool drain + session finalize) on every machine we've
# tested; override via ``HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S`` if a
# slower environment needs more headroom (e.g. encrypted disks
# flushing checkpoints) and accept that a longer grace also means a
# longer wait when shutdown actually deadlocks.
_DEFAULT_SHUTDOWN_GRACE_S = 1.0


def _shutdown_grace_seconds() -> float:
    raw = (os.environ.get("HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S") or "").strip()
    if not raw:
        return _DEFAULT_SHUTDOWN_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SHUTDOWN_GRACE_S
    return value if value > 0 else _DEFAULT_SHUTDOWN_GRACE_S


def _log_signal(signum: int, frame) -> None:
    """Capture WHICH thread and WHERE a termination signal hit us.

    SIG_DFL for SIGPIPE kills the process silently the instant any
    background thread (TTS playback, beep, voice status emitter, etc.)
    writes to a stdout the TUI has stopped reading.  Without this
    handler the gateway-exited banner in the TUI has no trace — the
    crash log never sees a Python exception because the kernel reaps
    the process before the interpreter runs anything.

    Termination semantics: ``sys.exit(0)`` here used to race the worker
    pool — a thread holding ``_stdout_lock`` mid-flush would block the
    interpreter shutdown indefinitely.  We now log the stack, give the
    process the configured shutdown grace
    (``HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S``, default
    ``_DEFAULT_SHUTDOWN_GRACE_S``) to drain naturally on a background
    thread, and fall back to ``os._exit(0)`` so a wedged write/flush
    can never strand the process.
    """
    # SIGPIPE and SIGHUP don't exist on Windows — build the lookup
    # dict from attributes that actually exist on the current platform.
    _signal_names: dict[int, str] = {}
    for _attr in ("SIGPIPE", "SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK"):
        _sig = getattr(signal, _attr, None)
        if _sig is not None:
            _signal_names[int(_sig)] = _attr
    name = _signal_names.get(signum, f"signal {signum}")
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== {name} received · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
            if frame is not None:
                f.write("main-thread stack at signal delivery:\n")
                traceback.print_stack(frame, file=f)
            # All live threads — signal may have been triggered by a
            # background thread (write to broken stdout from TTS, etc.).
            import threading as _threading
            for tid, th in _threading._active.items():
                f.write(f"\n--- thread {th.name} (id={tid}) ---\n")
                f.write("".join(traceback.format_stack(sys._current_frames().get(tid))))
    except Exception:
        pass
    print(f"[gateway-signal] {name}", file=sys.stderr, flush=True)

    import threading as _threading

    def _hard_exit() -> None:
        # If a worker thread is still mid-flush on a half-closed pipe,
        # ``sys.exit(0)`` would wait forever for it to drop the GIL on
        # interpreter shutdown.  ``os._exit`` skips atexit handlers but
        # breaks the deadlock.  The crash log + stderr line above are
        # the forensic trail.
        os._exit(0)

    timer = _threading.Timer(_shutdown_grace_seconds(), _hard_exit)
    timer.daemon = True
    timer.start()

    # ── Flush sessions before exit ───────────────────────────────────
    # The atexit handler (_shutdown_sessions) is registered in
    # tui_gateway/server.py, but a worker thread holding the GIL or
    # _stdout_lock can block atexit from completing within the grace
    # window.  Explicitly finalize sessions here so that unpersisted
    # messages reach state.db before the hard-exit timer fires.
    try:
        from tui_gateway.server import _shutdown_sessions

        _shutdown_sessions()
    except Exception:
        pass

    try:
        sys.exit(0)
    except SystemExit:
        # Re-raise so the main-thread interpreter unwinds and runs
        # atexit + finalisers inside the grace window.  Python signal
        # handlers always run on the main thread, but a worker thread
        # holding ``_stdout_lock`` mid-flush can keep that unwind
        # waiting indefinitely; the daemon timer above is the safety
        # net for that exact case.
        raise


# SIGPIPE: ignore, don't exit. The old SIG_DFL killed the process
# silently whenever a *background* thread (TTS playback chain, voice
# debug stderr emitter, beep thread) wrote to a pipe the TUI had gone
# quiet on — even though the main thread was perfectly fine waiting on
# stdin.  Ignoring the signal lets Python raise BrokenPipeError on the
# offending write (write_json already handles that with a clean
# sys.exit(0) + _log_exit), which keeps the gateway alive as long as
# the main command pipe is still readable.  Terminal signals still
# route through _log_signal so kills and hangups are diagnosable.
#
# SIGPIPE and SIGHUP don't exist on Windows; guard each installation
# with hasattr so ``python -m tui_gateway.entry`` (spawned by
# ``hermes --tui``) imports cleanly there.  SIGBREAK (Windows' Ctrl+Break)
# is installed when available as a weaker equivalent of SIGHUP.
#
# signal.signal() is only legal in the MAIN thread. On the Desktop/WebSocket
# agent-build path, server._build() runs in a daemon thread and does
# ``from tui_gateway.entry import ensure_mcp_discovery_started`` as the first
# import of entry (entry.main() is never run there), which used to raise
# "ValueError: signal only works in main thread of the main interpreter" and
# abort MCP discovery startup.  Install each handler only when we're in the
# main thread: handlers are process-global, so a main-thread import anywhere
# in the process still installs them for everyone, and an off-thread import
# (Desktop build path) simply no-ops instead of crashing the import.  This
# preserves the original SIG_IGN/SIG_DFL behavior on the classic TUI/serve
# path while fixing the off-thread import crash.


def _install_signal(signame, handler):
    """Install a signal handler if legal in this thread.

    signal.signal() raises ValueError outside the main thread; skip silently
    there so a worker-thread import of this module (Desktop build path) does
    not abort.  On any main-thread import the handler is installed as before.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    sig = getattr(signal, signame, None)
    if sig is None:
        return  # Windows: SIGPIPE/SIGHUP absent
    try:
        signal.signal(sig, handler)
    except (ValueError, OSError, RuntimeError):
        # Not in the main thread despite the check, or handler rejected.
        # Skip rather than crash the import (see above).
        pass


_install_signal("SIGPIPE", signal.SIG_IGN)
_install_signal("SIGTERM", _log_signal)
if hasattr(signal, "SIGHUP"):
    _install_signal("SIGHUP", _log_signal)
elif hasattr(signal, "SIGBREAK"):
    # Windows-only: Ctrl+Break in a console window delivers SIGBREAK.
    # Route it through the same handler so kills are diagnosable.
    _install_signal("SIGBREAK", _log_signal)
_install_signal("SIGINT", signal.SIG_IGN)


def _log_exit(reason: str) -> None:
    """Record why the gateway subprocess is shutting down.

    Three exit paths (startup write fail, parse-error-response write fail,
    dispatch-response write fail, stdin EOF) all collapse into a silent
    sys.exit(0) here.  Without this trail the TUI shows "gateway exited"
    with no actionable clue about WHICH broken pipe or WHICH message
    triggered it — the main reason voice-mode turns look like phantom
    crashes when the real story is "TUI read pipe closed on this event".
    """
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== gateway exit · {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"· reason={reason} ===\n"
            )
    except Exception:
        pass
    print(f"[gateway-exit] {reason}", file=sys.stderr, flush=True)


def wait_for_mcp_discovery(timeout: "float | None" = None) -> None:
    """Block until background MCP discovery finishes, up to the resolved bound.

    MCP discovery runs in a daemon thread spawned at startup (see main()) so a
    slow/dead server can't freeze ``gateway.ready``.  But the agent snapshots
    its tool list ONCE at build time and never re-reads it, so a reachable-but-
    slow server that finishes connecting *after* the first prompt would be
    invisible for the whole session.  Joining with a bounded timeout before the
    first agent build lets already-spawning servers land without re-introducing
    the startup hang: ``thread.join(timeout)`` returns the instant discovery
    completes (so fast/no-MCP startups pay ~0s), and a dead server is simply not
    waited on beyond the bound.  No-op when no discovery thread was started.

    The bound comes from ``mcp_discovery_timeout`` in config (shared with the
    CLI path via ``hermes_cli.mcp_startup``); ``timeout`` overrides it.
    """
    thread = _mcp_discovery_thread
    if thread is not None and thread.is_alive():
        try:
            from hermes_cli.mcp_startup import _resolve_discovery_timeout

            bound = _resolve_discovery_timeout(timeout)
        except Exception:
            bound = timeout if timeout is not None else 0.75
        thread.join(timeout=bound)
        return
    # Discovery is spawned via the shared owner (ensure_mcp_discovery_started
    # → hermes_cli.mcp_startup); wait on it so the first agent build still
    # catches fast servers. Re-invoke the idempotent spawn first: if the
    # previous run finished with zero connected servers,
    # start_background_mcp_discovery's retry-after-zero-connected allowance
    # kicks off a fresh discovery run here instead of leaving the process
    # latched MCP-less for the session. In multi-profile processes this
    # retry runs under the CALLER's profile context (agent build binds the
    # session profile's HERMES_HOME first), so a launch profile with no
    # mcp_servers no longer starves selected profiles of discovery (#67605).
    # Gated on _mcp_discovery_enabled so non-MCP sessions never pay the
    # tools.mcp_tool import on the per-agent-build wait path.
    if not _mcp_discovery_enabled:
        return
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(
            logger=logger, thread_name="tui-mcp-discovery"
        )
    except Exception:
        logger.debug(
            "TUI MCP discovery retry-spawn failed", exc_info=True
        )
    try:
        from hermes_cli.mcp_startup import (
            wait_for_mcp_discovery as _startup_wait,
        )

        _startup_wait(timeout)
    except Exception:
        pass


def mcp_discovery_in_flight() -> bool:
    """Return True if ANY background MCP discovery thread is still running.

    Used by the agent-build path to decide whether to schedule a late tool
    snapshot refresh: if discovery didn't land within the bounded
    ``wait_for_mcp_discovery`` join, the agent was built without those tools
    and the banner/tool count will be stale until they arrive.

    There are two independent discovery-thread owners by surface: the stdio
    ``hermes --tui`` path spawns ITS thread here (``_mcp_discovery_thread``),
    while the desktop app + dashboard WebSocket sidecar (``tui_gateway/ws.py``)
    and ``hermes dashboard`` spawn theirs via
    ``hermes_cli.mcp_startup.start_background_mcp_discovery``. The late-refresh
    scheduler imports this function regardless of surface, so it MUST consult
    both — checking only the entry thread left the desktop/dashboard surfaces
    with no late refresh, so a slow MCP server's tools never surfaced for the
    whole session (#51587).
    """
    thread = _mcp_discovery_thread
    if thread is not None and thread.is_alive():
        return True
    try:
        from hermes_cli.mcp_startup import (
            mcp_discovery_in_flight as _startup_in_flight,
        )

        return _startup_in_flight()
    except Exception:
        return False


def join_mcp_discovery(timeout: float | None = None) -> bool:
    """Block until background MCP discovery finishes, up to ``timeout`` seconds.

    Returns True if discovery has completed (both thread owners absent or no
    longer alive), False if either is still running after the timeout. Unlike
    ``wait_for_mcp_discovery`` this accepts an unbounded/long wait and reports
    the outcome, for the off-critical-path late-refresh waiter.

    Joins both discovery-thread owners (see ``mcp_discovery_in_flight``): the
    entry thread first, then the ``hermes_cli.mcp_startup`` thread used by the
    desktop/dashboard surfaces. ``timeout`` bounds EACH join, mirroring the
    pre-#51587 single-owner behavior for the entry thread.
    """
    entry_done = True
    thread = _mcp_discovery_thread
    if thread is not None:
        thread.join(timeout=timeout)
        entry_done = not thread.is_alive()
    try:
        from hermes_cli.mcp_startup import join_mcp_discovery as _startup_join

        startup_done = _startup_join(timeout=timeout)
    except Exception:
        startup_done = True
    return entry_done and startup_done


# Spurious stdin-EOF recovery tracker (shared open-file-description O_NONBLOCK flip).
_recovery_times: list[float] = []



def _has_configured_mcp_servers() -> bool:
    """Delegate to the shared native and portable MCP startup gate."""
    from hermes_cli.mcp_startup import _has_configured_mcp_servers as configured

    return configured()


def ensure_mcp_discovery_started() -> None:
    """Start background MCP discovery for the current profile context, once.

    ``main()`` calls this for the stdio/TUI path. WebSocket/Desktop
    entrypoints can accept sessions without running ``main()``, so the
    agent-build path (``server._start_agent_build``) also calls it AFTER
    binding the session profile's HERMES_HOME override — the shared owner in
    ``hermes_cli.mcp_startup`` captures the caller's context-local override
    and propagates it into the discovery thread, so discovery reads the
    SELECTED profile's ``mcp_servers``, not the launch profile's (#67605).

    Delegating to the shared owner (instead of a hand-rolled thread) keeps
    the process-wide start lock, the retry-after-zero-connected allowance,
    and interactive-OAuth suppression.

    Known limitation: MCP tool registration is process-global, so in a
    multi-profile process the FIRST profile that builds an agent wins the
    discovery slot. Full per-profile MCP registries are tracked in #67605.
    """
    global _mcp_discovery_enabled

    if not _has_configured_mcp_servers():
        return
    _mcp_discovery_enabled = True
    try:
        from hermes_cli.mcp_startup import start_background_mcp_discovery

        start_background_mcp_discovery(
            logger=logger, thread_name="tui-mcp-discovery"
        )
    except Exception:
        logger.warning(
            "Background MCP tool discovery failed to start", exc_info=True
        )


def main():
    output = sys.stdout
    output_lock = threading.Lock()

    def emit(value: dict) -> bool:
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            with output_lock:
                output.write(encoded + "\n")
                output.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    shell = AccountAuthShell(_RuntimeAuth(), _ServerGateway(), emit)
    try:
        shell.start()
    except AuthRequired as error:
        if not emit(_auth_event("auth.status", _UnavailableStatus(error.reason))):
            _log_exit("startup write failed (broken stdout pipe before first event)")
            sys.exit(0)

    monitor_stop = threading.Event()

    def monitor_auth_owner() -> None:
        while not monitor_stop.wait(5.0):
            shell.poll()

    threading.Thread(
        target=monitor_auth_owner,
        name="tui-auth-monitor",
        daemon=True,
    ).start()

    try:
        while True:
            raw = sys.stdin.readline()
            if not raw:
                # Stdin fell through — check if spurious (O_NONBLOCK flip by a
                # child on the shared open file description) or genuine EOF.
                if not handle_spurious_eof(_recovery_times, _log_exit):
                    break
                continue

            line = raw.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                if not emit({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None}):
                    _log_exit("parse-error-response write failed (broken stdout pipe)")
                    sys.exit(0)
                continue

            method = req.get("method") if isinstance(req, dict) else None
            resp = shell.dispatch(req)
            if resp is not None:
                if not emit(resp):
                    _log_exit(f"response write failed for method={method!r} (broken stdout pipe)")
                    sys.exit(0)
    finally:
        monitor_stop.set()


if __name__ == "__main__":
    main()
