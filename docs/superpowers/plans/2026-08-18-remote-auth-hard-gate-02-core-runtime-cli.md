# Hermes Remote Auth Core Runtime and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared Python authentication owner/runtime and make every Python/CLI entry fail closed before Hermes capability imports or side effects.

**Architecture:** `client_auth/__init__.py` is empty and `guard.py` is stdlib-only, so raw argv is classified before recovery, profiles, config, parser, plugins, or third-party imports. `client.py` owns the fixed Django HTTP contract; `runtime.py` owns one state machine with `VaultOwner` and `MemoryOwner`; `bridge.py` exposes only three JSONL verbs for Desktop. Entrypoint discovery, real locked-start tests, and a guard at the shared `model_tools.handle_function_call` dispatch chokepoint prevent direct-script and indirect-tool bypasses.

**Tech Stack:** Python 3.11+, httpx 0.28.1, keyring 25.7.0, pywin32, Unix sockets, Windows Named Pipes, pytest.

---

## File map

- Create: `hermes_cli/client_auth/__init__.py`
- Create: `hermes_cli/client_auth/guard.py`
- Create: `hermes_cli/client_auth/runtime.py`
- Create: `hermes_cli/client_auth/client.py`
- Create: `hermes_cli/client_auth/bridge.py`
- Create: `hermes_cli/client_auth/entrypoints.json`
- Create: `hermes_cli/client_auth/static_help.txt`
- Create: `hermes_cli/subcommands/provider.py`
- Create: `scripts/check_auth_entrypoints.py`
- Create: `scripts/generate_auth_free_help.py`
- Create: `tests/hermes_cli/client_auth/test_client.py`
- Create: `tests/hermes_cli/client_auth/test_runtime.py`
- Create: `tests/hermes_cli/client_auth/test_guard.py`
- Create: `tests/hermes_cli/client_auth/test_bridge.py`
- Create: `tests/hermes_cli/client_auth/test_entrypoints.py`
- Create: `tests/hermes_cli/client_auth/test_account_commands.py`
- Create: `tests/hermes_cli/client_auth/test_boundaries.py`
- Modify: `pyproject.toml`, `uv.lock`, `hermes_cli/main.py`, `hermes_cli/_startup_fast.py`, `hermes_cli/subcommands/auth.py`, `hermes_cli/subcommands/login.py`, `hermes_cli/subcommands/logout.py`, `run_agent.py`, `acp_adapter/entry.py`, `tui_gateway/entry.py`, `mcp_serve.py`
- Modify boundary files: `model_tools.py`, `agent/tool_executor.py`, `agent/agent_runtime_helpers.py`, `agent/transports/hermes_tools_mcp_server.py`, `gateway/platforms/api_server.py`, `cron/scheduler.py`, `hermes_cli/web_server.py`, `acp_adapter/server.py`
- Modify boundary tests: `tests/test_model_tools.py`, `tests/test_mcp_serve.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py`, `tests/integration/test_batch_runner.py`, `tests/test_mini_swe_runner.py`

### Task 1: Implement the fixed Django HTTP client

- [x] **Step 1: Add the pinned vault dependency**

Add to `[project].dependencies` in `pyproject.toml`:

```toml
  "keyring==25.7.0",
```

Run:

```bash
uv lock
```

Expected: `uv.lock` contains keyring 25.7.0 and its platform dependencies; no authentication code imports keyring before `guard.py` passes.

- [x] **Step 2: Write failing client contract tests**

Create `tests/hermes_cli/client_auth/test_client.py` with tests that use `httpx.MockTransport` and assert:

```python
def test_login_uses_only_fixed_origin_and_cookie_names():
    client, requests = make_client([
        html_response(200, csrf="csrf-1", cookie="agent_history_csrftoken=csrf-1; Secure; Path=/agent/"),
        redirect_response(302, cookie="agent_history_sessionid=session-1; Secure; HttpOnly; Path=/agent/"),
        json_response(200, {
            "authenticated": True,
            "username": "alice",
            "server_time": "2026-08-18T12:00:00+00:00",
            "session_expires_at": "2026-09-01T12:00:00+00:00",
        }),
    ])
    result = client.login("alice", bytearray(b"secret"))
    assert result.username == "alice"
    assert [r.url.path for r in requests] == [
        "/agent/accounts/login/",
        "/agent/accounts/login/",
        "/agent/api/session/",
    ]
    assert set(result.cookies) == {
        "agent_history_sessionid", "agent_history_csrftoken"
    }


@pytest.mark.parametrize("bad", [
    html_response(200),
    redirect_response(302, location="https://evil.example/"),
    json_response(200, {"authenticated": True}),
])
def test_status_rejects_html_cross_origin_and_schema_drift(bad):
    client, _ = make_client([bad])
    with pytest.raises(AuthServiceError):
        client.status(valid_cookie_record())
```

The helpers return real `httpx.Response` objects; keep all fixtures in the test file so production code is not shaped around test switches.

- [x] **Step 3: Run the client test and verify it fails**

Run:

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_client.py -q
```

Expected: FAIL because `hermes_cli.client_auth.client` does not exist.

- [x] **Step 4: Implement the client public API**

Create `hermes_cli/client_auth/client.py` with these fixed public types and methods:

```python
AUTH_ORIGIN = "https://c2sml.cn"
AUTH_PREFIX = "/agent"
LOGIN_PATH = f"{AUTH_PREFIX}/accounts/login/"
LOGOUT_PATH = f"{AUTH_PREFIX}/accounts/logout/"
SESSION_PATH = f"{AUTH_PREFIX}/api/session/"
SESSION_COOKIE = "agent_history_sessionid"
CSRF_COOKIE = "agent_history_csrftoken"

@dataclass(frozen=True)
class CookieRecord:
    cookies: dict[str, str]
    username: str
    session_expires_at: str

@dataclass(frozen=True)
class SessionStatus:
    username: str
    server_time: str
    session_expires_at: str

class AuthClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=AUTH_ORIGIN,
            transport=transport,
            follow_redirects=False,
            timeout=15.0,
        )

    def login(self, username: str, password: bytearray) -> CookieRecord:
        login_page = self._request("GET", LOGIN_PATH)
        csrf = _extract_csrf(login_page.text)
        response = self._request(
            "POST",
            LOGIN_PATH,
            data={
                "csrfmiddlewaretoken": csrf,
                "username": username,
                "password": password.decode("utf-8"),
            },
            headers={"Referer": f"{AUTH_ORIGIN}{LOGIN_PATH}"},
        )
        _require_same_origin_redirect(response)
        cookies = _validated_cookie_record(self._http.cookies)
        status = self.status(cookies)
        return CookieRecord(cookies, status.username, status.session_expires_at)

    def status(self, cookies: dict[str, str]) -> SessionStatus:
        response = self._request("GET", SESSION_PATH, cookies=cookies)
        return _parse_session_status(response)

    def logout(self, cookies: dict[str, str]) -> None:
        csrf = cookies.get(CSRF_COOKIE, "")
        try:
            self._request(
                "POST", LOGOUT_PATH,
                data={"csrfmiddlewaretoken": csrf},
                cookies=cookies,
                headers={"Referer": f"{AUTH_ORIGIN}{LOGIN_PATH}"},
            )
        finally:
            self._http.cookies.clear()
```

Implement `_request`, `_extract_csrf`, `_require_same_origin_redirect`, `_validated_cookie_record`, and `_parse_session_status` in the same module. They must reject non-HTTPS/cross-origin redirects, non-JSON status responses, unknown/missing schema keys, Cookie values without `Secure` and `/agent/` path, and any status other than the documented `200`, `401`, `429`, and service-error classes.

- [x] **Step 5: Run tests and commit**

Run:

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_client.py -q
git diff --check
git add pyproject.toml uv.lock hermes_cli/client_auth/client.py tests/hermes_cli/client_auth/test_client.py
git commit -m "feat: add fixed django auth client"
```

Expected: all client tests pass and no password/Cookie appears in exception text.

### Task 2: Define the single runtime protocol and revocation semantics

- [x] **Step 1: Write failing state-machine tests**

Create `tests/hermes_cli/client_auth/test_runtime.py` covering:

```python
def test_new_owner_uses_fresh_instance_and_old_scope_never_revives():
    first = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    old_scope = first.scope
    second = RuntimeSnapshot.new_authenticated("alice", now=20.0, ttl=60.0)
    assert first.runtime_instance_id != second.runtime_instance_id
    with pytest.raises(AuthRequired):
        second.require_authorized("tool", expected=old_scope, now=21.0)


def test_dead_liveness_connection_overrides_cached_authenticated_state():
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    consumer = FakeConsumer(state.snapshot(), alive=False)
    with pytest.raises(AuthRequired) as error:
        consumer.require_authorized("gateway.request", now=11.0)
    assert error.value.reason == "runtime_unavailable"


def test_expiry_and_epoch_comparison_fail_closed():
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    with pytest.raises(AuthRequired):
        state.require_authorized("worker", expected=AuthScope(state.runtime_instance_id, state.epoch + 1), now=11.0)
    with pytest.raises(AuthRequired):
        state.require_authorized("worker", expected=state.scope, now=70.0)
```

- [x] **Step 2: Run the test and verify it fails**

Run:

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py -q
```

Expected: FAIL because `runtime.py` and its types do not exist.

- [x] **Step 3: Implement immutable state and one authorization primitive**

Create `hermes_cli/client_auth/__init__.py` as an empty file (comments/docstring only if necessary; no imports or re-exports). Create `hermes_cli/client_auth/runtime.py` with the shared `AuthState`, `AuthScope`, and `RuntimeSnapshot` types from the master plan, plus:

```python
AUTH_EXIT_CODE = 20
LEASE_SECONDS = 60.0

class AuthRequired(RuntimeError):
    code = "AUTH_REQUIRED"

    def __init__(self, reason: str | None = None):
        super().__init__(reason or self.code)
        self.reason = reason

@dataclass(frozen=True)
class RuntimeSnapshot:
    state: AuthState
    epoch: int
    valid_until: float
    runtime_instance_id: str
    boot_id: str
    username: str | None
    session_expires_at: str | None
    reason: str | None

    @classmethod
    def new_authenticated(cls, username: str, *, now: float, ttl: float):
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=1,
            valid_until=now + ttl,
            runtime_instance_id=secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=username,
            session_expires_at=None,
            reason=None,
        )

    @property
    def scope(self) -> AuthScope:
        return AuthScope(self.runtime_instance_id, self.epoch)

    def require_authorized(self, boundary: str, *, expected: AuthScope, now: float):
        del boundary
        if self.state is not AuthState.AUTHENTICATED:
            raise AuthRequired(self.reason)
        if now >= self.valid_until or _read_boot_id() != self.boot_id:
            raise AuthRequired("session_expired")
        if expected != self.scope:
            raise AuthRequired("runtime_unavailable")
        return self.scope
```

Add a process-global consumer that keeps an atomic snapshot, a dedicated reader thread/task, and an exclusive owner connection. `require_authorized(boundary, expected=None)` must perform a zero-timeout liveness probe before reading the snapshot and must lock on EOF, reader death, boot mismatch, deadline expiry, schema failure, or exact scope mismatch.

`expected=None` is permitted only for the immediate entry/liveness check in the same process that just obtained the current snapshot. Every queued, deferred, concurrent, tokenized, or cross-process boundary—including workers, delegates, kanban jobs, MCP/ACP requests, gateway messages, cron jobs, and tool calls—must capture and pass the exact `AuthScope`; omitting it at those boundaries is a programming error that fails closed.

When converting the server response to a lease, parse both `server_time` and `session_expires_at` as timezone-aware datetimes, compute `absolute_remaining = session_expires_at - server_time`, and set `valid_until = min(monotonic_now + LEASE_SECONDS, monotonic_now + absolute_remaining)`. Retain the original absolute timestamp for the owner generation; refresh may shorten this deadline but can never extend it past that timestamp. Non-positive remaining time locks immediately. Wall-clock changes after conversion never lengthen the monotonic deadline.

- [x] **Step 4: Add child-handle non-inheritance tests**

Add behavior tests that spawn a child while the consumer is connected and assert the child cannot keep the owner connection alive after the parent closes it. Use real POSIX `close_fds`, Windows handle-list behavior on a native Windows runner, and a Node/PTY integration case in Plan 3; never fake `sys.platform`.

- [x] **Step 5: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py -q
git diff --check
git add hermes_cli/client_auth/__init__.py hermes_cli/client_auth/runtime.py tests/hermes_cli/client_auth/test_runtime.py
git commit -m "feat: add revocable auth runtime protocol"
```

### Task 3: Add VaultOwner, MemoryOwner, native IPC, and hardening

- [x] **Step 1: Add failing owner parity tests**

Extend `test_runtime.py` with a parameterized owner contract:

```python
@pytest.mark.parametrize("owner_factory", [vault_owner_factory, memory_owner_factory])
def test_owner_login_status_logout_and_rotation_are_identical(owner_factory):
    owner, secret_backend, auth_client = owner_factory()
    snapshot = owner.login("alice", bytearray(b"secret"))
    assert snapshot.state is AuthState.AUTHENTICATED
    assert secret_backend.read_count <= 1
    rotated = owner.refresh()
    assert rotated.runtime_instance_id == snapshot.runtime_instance_id
    assert rotated.epoch == snapshot.epoch
    owner.logout()
    assert owner.snapshot().state is AuthState.SIGNED_OUT
    assert secret_backend.read() is None


def test_memory_owner_rejects_authentication_when_required_hardening_fails():
    owner = memory_owner_factory(hardener=FailingHardener("core_dump"))[0]
    with pytest.raises(AuthRequired) as error:
        owner.login("alice", bytearray(b"secret"))
    assert error.value.reason == "runtime_unavailable"


def test_graphical_vault_failure_never_falls_back_to_memory_or_file(tmp_path):
    owner = vault_owner_factory(
        vault=UnavailableVault(), forbidden_fallback_root=tmp_path
    )[0]
    with pytest.raises(AuthRequired) as error:
        owner.login("alice", bytearray(b"secret"))
    assert error.value.reason == "vault_unavailable"
    assert list(tmp_path.iterdir()) == []


def test_profiles_share_one_os_user_runtime_and_logout_revokes_both(tmp_path):
    owner = memory_owner_factory(runtime_identity="current-os-user")[0]
    coder = connect_consumer(owner, hermes_home=tmp_path / "coder")
    writer = connect_consumer(owner, hermes_home=tmp_path / "writer")
    scope = owner.login("alice", bytearray(b"secret")).scope
    assert coder.require_authorized("profile.coder", expected=scope) == scope
    assert writer.require_authorized("profile.writer", expected=scope) == scope
    owner.logout()
    with pytest.raises(AuthRequired):
        coder.require_authorized("profile.coder", expected=scope)
    with pytest.raises(AuthRequired):
        writer.require_authorized("profile.writer", expected=scope)


def test_logout_locks_and_clears_secret_even_when_remote_logout_fails():
    owner, secret_backend, auth_client = vault_owner_factory()
    before = owner.login("alice", bytearray(b"secret"))
    auth_client.logout_error = AuthServiceError("server_unavailable")
    after = owner.logout()
    assert after.state is AuthState.SIGNED_OUT
    assert after.epoch == before.epoch + 1
    assert secret_backend.read() is None
```

- [x] **Step 2: Run the owner tests and verify they fail**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py -q
```

Expected: FAIL because owner and secret-backend behavior is absent.

- [x] **Step 3: Implement the two private owners inside `runtime.py`**

Use one owner interface:

```python
class _SecretOwner(Protocol):
    def login(self, username: str, password: bytearray) -> RuntimeSnapshot:
        raise NotImplementedError

    def refresh(self) -> RuntimeSnapshot:
        raise NotImplementedError

    def logout(self) -> RuntimeSnapshot:
        raise NotImplementedError

    def snapshot(self) -> RuntimeSnapshot:
        raise NotImplementedError

class VaultOwner:
    SERVICE = "cn.c2sml.hermes.remote-auth"
    ACCOUNT = "django-session"

    def _load_record(self) -> CookieRecord | None:
        raw = keyring.get_password(self.SERVICE, self.ACCOUNT)
        return _decode_cookie_blob(raw) if raw else None

    def _store_record(self, record: CookieRecord) -> None:
        keyring.set_password(self.SERVICE, self.ACCOUNT, _encode_cookie_blob(record))

    def _delete_record(self) -> None:
        try:
            keyring.delete_password(self.SERVICE, self.ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            return

class MemoryOwner:
    def __init__(self, client: AuthClient, hardener: ProcessHardener):
        self._client = client
        self._hardener = hardener
        self._record: CookieRecord | None = None
```

`VaultOwner` and `MemoryOwner` call the same `_OwnerCore` for login, refresh, epoch changes, Session expiry, local login rate limiting, 15-minute idle exit, and consumer publication. Only the three storage methods differ. A successful VaultOwner write is one versioned JSON blob; a failed write leaves the prior blob readable.

The owner schedules validation 57–60 seconds after each success using an injected CSPRNG jitter source, always at or before the current `valid_until`. A transient retry may occur only before that same deadline and never changes it. If no success arrives by the deadline, or the server returns TLS, network, 5xx, 429, invalid schema, or invalid Session at the scheduled check, increment epoch and lock immediately; there is no extra grace. Tests use an injected monotonic clock and jitter source, not real sleeps.

Owner mode is not a public option. Resolution always tries to connect to any already-live owner for the current OS user first, regardless of whether the caller is graphical, SSH, or container-launched. Only when no live owner exists does the resolver elect a new one: the packaged Desktop local bridge creates `VaultOwner`; an SSH remote bridge, container service, interactive CLI under `SSH_CONNECTION`, or genuinely headless Linux session creates `MemoryOwner`; a local macOS/Windows desktop session and a graphical Linux session create `VaultOwner`. This preserves exactly one owner per OS user and avoids rejecting a valid live `VaultOwner` merely because the newest caller arrived through SSH. If a newly elected graphical owner's vault is locked or unavailable, authentication fails with `vault_unavailable` and never falls back to MemoryOwner or a file. Mode remains fixed for that owner generation and is published in its non-secret state record. Tests cover live-owner-first resolution and every owner-election input without allowing a CLI flag, config key, `HERMES_HOME`, or server URL override.

- [x] **Step 4: Implement native owner endpoints without fallback transport**

Inside `runtime.py`, use:

```python
def runtime_endpoint() -> RuntimeEndpoint:
    if os.name == "nt":
        return WindowsNamedPipeEndpoint.for_current_sid(first_instance=True)
    return UnixEndpoint.for_current_user(
        random_name=secrets.token_hex(16),
        forbid_abstract=sys.platform.startswith("linux"),
        darwin_user_temp=sys.platform == "darwin",
    )
```

Unix requirements: `0700` directory, `0600` pointer record and Socket, random Socket name, `SO_PEERCRED` on Linux, `getpeereid()` on macOS, `_CS_DARWIN_USER_TEMP_DIR` on macOS, and stale unlink only while holding the unique owner lock after proving no live peer. Windows requirements: SID-derived name, `FILE_FLAG_FIRST_PIPE_INSTANCE`, DACL restricted to current SID and SYSTEM, server impersonation, and token-SID verification. Any unavailable primitive raises `AuthRequired("runtime_unavailable")`; ordinary files and TCP are forbidden as transport.

- [x] **Step 5: Implement required MemoryOwner hardening**

Add `ProcessHardener.apply_required()` with native branches that execute before Cookie acquisition:

```python
def apply_required(self) -> None:
    if sys.platform.startswith("linux"):
        _linux_prctl_dumpable_zero()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    elif sys.platform == "darwin":
        _darwin_deny_attach()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    elif os.name == "nt":
        _windows_disable_wer_dump()
        _windows_apply_process_mitigations()
    else:
        raise AuthRequired("runtime_unavailable")
```

All owner/consumer handles use `CLOEXEC` or non-inheritable Windows handle lists. Locking Cookie pages is best-effort and records a redacted diagnostic flag; required hardening failure prevents `authenticated`. Crash/report/log formatters receive only state, reason, username, epoch, and timestamps.

- [x] **Step 6: Run native and parity tests, then commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py -q
git diff --check
git add hermes_cli/client_auth/runtime.py tests/hermes_cli/client_auth/test_runtime.py
git commit -m "feat: add vault and memory auth owners"
```

Expected: parity tests pass on the host; transport and hardening tests marked for native OS run only on their actual platform.

### Task 4: Add the exact raw-argv gate and static help

- [x] **Step 1: Write the exhaustive failing argv table**

Create `tests/hermes_cli/client_auth/test_guard.py`:

```python
ALLOWED = [
    ["login"],
    ["logout"],
    ["auth", "status"],
    ["--help"], ["-h"],
    ["--version"], ["-V"],
]

@pytest.mark.parametrize("argv", ALLOWED)
def test_exact_unauthenticated_shapes(argv):
    assert classify_raw_argv(argv).auth_free is True

@pytest.mark.parametrize("argv", [
    [], ["login", "--provider", "nous"], ["logout", "--"],
    ["auth"], ["auth", "status", "extra"], ["--help", "extra"],
    ["-hV"], ["--version", "--help"], ["doctor"], ["gateway", "status"],
])
def test_every_shape_variant_is_protected(argv):
    assert classify_raw_argv(argv).auth_free is False
```

Add a subprocess test importing `hermes_cli.main` with locked runtime paths and assert recovery/profile/config/parser modules are absent from `sys.modules` when a protected command exits `20`. Extend the import-weight test to import both `hermes_cli.client_auth` and `hermes_cli.client_auth.guard`; `client_auth/__init__.py` must remain empty (comments/docstring only) with no re-exports or third-party imports, because Python executes it before loading `guard.py`.

- [x] **Step 2: Run and verify the red state**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_guard.py -q
```

Expected: FAIL because `guard.py` does not exist.

- [x] **Step 3: Implement stdlib-only classification**

Create `hermes_cli/client_auth/guard.py` with no imports outside the standard library:

```python
AUTH_FREE = {
    ("login",),
    ("logout",),
    ("auth", "status"),
    ("--help",), ("-h",),
    ("--version",), ("-V",),
}

@dataclass(frozen=True)
class GuardDecision:
    auth_free: bool
    interactive: bool

def classify_raw_argv(argv: Sequence[str]) -> GuardDecision:
    shape = tuple(argv)
    return GuardDecision(
        auth_free=shape in AUTH_FREE,
        interactive=sys.stdin.isatty() and sys.stderr.isatty(),
    )

def enforce_raw_argv(argv: Sequence[str]) -> None:
    decision = classify_raw_argv(argv)
    if decision.auth_free:
        return
    from hermes_cli.client_auth.runtime import authorize_entrypoint
    authorize_entrypoint("cli.start", interactive=decision.interactive)
```

`authorize_entrypoint` first performs online validation. If the state is signed out/locked and `interactive=True`, it reads the username and password with `input`/`getpass`, calls the same rate-limited owner login once, wipes the password `bytearray`, and then re-runs `require_authorized` before returning. It never prompts for noninteractive callers, `rate_limited`, or service failures. Catch `AuthRequired` only in the top-level bootstrap wrapper, print structured `AUTH_REQUIRED` to stderr, and exit `20`. Do not turn runtime import failure into authorization.

- [x] **Step 4: Generate static help from the real parser**

Create `scripts/generate_auth_free_help.py` that imports the real parser in a build/test process, captures `parser.format_help()`, and atomically writes `hermes_cli/client_auth/static_help.txt`. Add `scripts/generate_auth_free_help.py --check` to compare without writing. Update `hermes_cli/main.py` so the only pre-guard help path reads this packaged text file using stdlib path operations.

- [x] **Step 5: Reorder `hermes_cli/main.py` without dropping bootstrap behavior**

Preserve these existing startup operations byte-for-byte unless relocation is required: the `try/except ModuleNotFoundError` around `hermes_bootstrap`, the import **and call** to `suppress_platform_ver_console()`, `os`/`sys`, the inline script-mode project-root bootstrap, and the `_ensure_project_root_on_path_fast()` behavior. Immediately after that stdlib-only safe bootstrap, handle `try_fast_version()` and packaged static help, then import `client_auth.guard` and call `enforce_raw_argv(sys.argv[1:])`.

Only after the guard succeeds may `main.py` import/call `_early_recovery`, define or execute full recovery routines, parse profile/config, import argparse/subcommands, load dotenv, or perform any process/network/SessionDB side effect. Move the existing early-recovery import and call behind enforcement; do not replace the guarded bootstrap with the shortened illustrative sequence from this plan.

- [x] **Step 6: Run guard, startup, and help parity tests, then commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_guard.py tests/hermes_cli/test_startup_fast_guards.py tests/test_project_metadata.py -q
../../.venv/bin/python scripts/generate_auth_free_help.py --check
git diff --check
git add hermes_cli/client_auth/guard.py hermes_cli/client_auth/static_help.txt scripts/generate_auth_free_help.py hermes_cli/main.py tests/hermes_cli/client_auth/test_guard.py
git commit -m "feat: enforce auth before cli startup"
```

### Task 5: Repurpose login/logout/status and migrate provider authentication

- [x] **Step 1: Write failing command-routing tests**

Create `tests/hermes_cli/client_auth/test_account_commands.py` that constructs the real parser and asserts:

```python
def test_account_commands_have_no_registration_or_server_flags(parser):
    assert parse(parser, ["login"]).command == "login"
    assert parse(parser, ["logout"]).command == "logout"
    assert parse(parser, ["auth", "status"]).auth_action == "status"
    for argv in (["login", "--server", "x"], ["login", "--register"], ["logout", "--provider", "nous"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)

def test_provider_commands_retain_old_provider_handlers(parser):
    args = parse(parser, ["provider", "status", "nous"])
    assert args.provider_action == "status"
    assert args.provider == "nous"
```

Add handler tests with a fake runtime: valid login is idempotent without `getpass`; signed-out login prompts once; logout increments epoch before remote logout; output states provider credentials were not modified. Add a subprocess case for `hermes login </dev/null` and assert structured `AUTH_REQUIRED`, exit `20`, and no traceback.

- [x] **Step 2: Run and verify the red state**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_account_commands.py -q
```

Expected: FAIL because current `auth` and `logout` still mean provider authentication.

- [x] **Step 3: Move provider parser semantics under `hermes provider`**

Create `hermes_cli/subcommands/provider.py` by moving the existing `auth` parser tree and renaming parser destinations to `provider_action`. Keep the existing provider handler function, injected as `cmd_provider`. Replace `hermes_cli/subcommands/auth.py` with only:

```python
def build_auth_parser(subparsers, *, cmd_auth_status):
    auth_parser = subparsers.add_parser("auth", help="Hermes account status")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action", required=True)
    status = auth_subparsers.add_parser("status", help="Show Hermes account status")
    status.set_defaults(func=cmd_auth_status)
```

Replace login/logout parsers with flag-free account commands. Register `provider` in `_SUBCOMMANDS`, completion metadata, generated static help, and the main parser.

- [x] **Step 4: Implement account command handlers**

In `hermes_cli/main.py`, route handlers to runtime methods:

```python
def cmd_login(_args):
    from hermes_cli.client_auth.runtime import AuthRequired, AuthState, account_login, account_status
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise AuthRequired("interactive_login_required")
    from getpass import getpass
    current = account_status()
    if current.state is AuthState.AUTHENTICATED:
        print(f"Authenticated as {current.username}")
        return
    username = input("Hermes account: ").strip()
    password = bytearray(getpass("Password: ").encode("utf-8"))
    try:
        result = account_login(username, password)
    finally:
        password[:] = b"\x00" * len(password)
    print(f"Authenticated as {result.username}")

def cmd_logout(_args):
    from hermes_cli.client_auth.runtime import account_logout
    account_logout()
    print("Remote Hermes account signed out; provider credentials were not modified.")

def cmd_auth_status(_args):
    from hermes_cli.client_auth.runtime import account_status
    print(json.dumps(account_status().public_dict(), sort_keys=True))
```

Never log input values. Interactive protected commands may call the same prompt flow before capability initialization; noninteractive commands return structured exit `20` and instruct `hermes login`.

- [x] **Step 5: Run routing and existing provider tests, then commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_account_commands.py tests/hermes_cli/test_auth_commands.py tests/hermes_cli/test_commands.py -q
git diff --check
git add hermes_cli/main.py hermes_cli/subcommands/auth.py hermes_cli/subcommands/login.py hermes_cli/subcommands/logout.py hermes_cli/subcommands/provider.py tests/hermes_cli/client_auth/test_account_commands.py
git commit -m "feat: make cli login manage hermes accounts"
```

### Task 6: Add the three-verb Desktop bridge

- [x] **Step 1: Write failing JSONL bridge tests**

Create `tests/hermes_cli/client_auth/test_bridge.py`:

```python
@pytest.mark.parametrize("request", [
    {"version": 1, "id": "1", "method": "signup", "params": {}},
    {"version": 1, "id": "1", "method": "login", "params": {"url": "https://evil"}},
    {"version": 1, "id": "1", "method": "exec", "params": {"command": "id"}},
])
def test_bridge_rejects_every_non_contract_operation(request):
    response = run_bridge_line(request)
    assert response["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "cookie" not in json.dumps(response).lower()

def test_login_response_contains_scope_but_no_secret():
    response = run_bridge_line({
        "version": 1, "id": "1", "method": "login",
        "params": {"username": "alice", "password": "secret"},
    })
    assert set(response["result"]) == {
        "state", "username", "runtime_instance_id", "epoch",
        "valid_until", "session_expires_at", "reason"
    }
    assert "secret" not in json.dumps(response)
    assert "cookie" not in json.dumps(response).lower()
```

- [x] **Step 2: Implement `bridge.py` with a closed dispatch table**

```python
METHODS = {
    "status": _status,
    "login": _login,
    "logout": _logout,
}
ALLOWED_PARAMS = {
    "status": frozenset(),
    "login": frozenset({"username", "password"}),
    "logout": frozenset(),
}

def dispatch(request: dict[str, object]) -> dict[str, object]:
    method = request.get("method")
    params = request.get("params", {})
    if method not in METHODS or not isinstance(params, dict):
        return error(request, "METHOD_NOT_ALLOWED")
    if set(params) != ALLOWED_PARAMS[method]:
        return error(request, "INVALID_PARAMS")
    return success(request, METHODS[method](params))
```

The stdio loop accepts one bounded JSON object per line, caps line length, returns one line, flushes, redacts all exception text, and never echoes params. Login converts password to `bytearray`, calls runtime login, wipes it best-effort, and drops references.

- [x] **Step 3: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_bridge.py -q
git diff --check
git add hermes_cli/client_auth/bridge.py tests/hermes_cli/client_auth/test_bridge.py
git commit -m "feat: add closed desktop auth bridge"
```

### Task 7: Discover, declare, and test every production entrypoint

- [x] **Step 1: Write scanner behavior tests against fixture trees**

Create `tests/hermes_cli/client_auth/test_entrypoints.py` with temporary Python, TS, JS, shell, pyproject, systemd, and s6 fixtures. Assert the scanner finds console scripts, `__main__`, main guards, spawn/exec targets, Electron/TUI backends, Docker entrypoints, and service installers. These tests exercise scanner behavior; they do not read production files and regex-assert source shape.

- [x] **Step 2: Implement scanner and production manifest comparison**

Create `scripts/check_auth_entrypoints.py` using `tomllib`, Python `ast`, and explicit parsers for package/service descriptors. Its result is a sorted set of stable entry IDs. Create `entrypoints.json` with objects shaped as:

```json
{"id":"pyproject:hermes","interactive":true,"startup":"guarded"}
{"id":"pyproject:hermes-agent","interactive":false,"startup":"guarded"}
{"id":"pyproject:hermes-acp","interactive":false,"startup":"guarded"}
{"id":"electron:primary-backend","interactive":false,"startup":"auth-shell"}
{"id":"tui:tui-gateway","interactive":true,"startup":"auth-shell"}
{"id":"docker:main-wrapper","interactive":false,"startup":"locked-waiting"}
```

Populate the file from the scanner's complete current output; each ID appears once. Record that root `registration_lifecycle.py` is provider/plugin replacement ownership, not user-account registration.

- [ ] **Step 3: Guard every manifest entry, including direct Python scripts**

Every production entry emitted by the scanner and recorded in `entrypoints.json` must receive the declared startup guard; no hand-written subset is authoritative. This includes current direct paths such as `run_agent.py`, `cli.py`, `batch_runner.py`, `mini_swe_runner.py`, `mcp_serve.py`, `toolsets.py`, `toolset_distributions.py`, `trajectory_compressor.py`, `gateway/run.py`, `agent/learning_graph.py`, `acp_adapter/entry.py`, and `tui_gateway/entry.py`, plus every shell/service/Electron target the scanner discovers. Use `enforce_raw_argv` or the noninteractive runtime preflight before capability imports. Electron and TUI auth-shell modes may start only their authentication transport; Agent/session/tool registration remains after an authenticated snapshot.

- [ ] **Step 4: Add real locked-start production tests**

For every manifest ID, launch the actual entry in a temporary locked runtime directory and instrument child process/network creation. Assert protected entries exit `20` or remain auth-only/`locked-waiting`, and capability imports, Popen/exec, Session DB reads, and non-auth sockets remain zero.

- [ ] **Step 5: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_entrypoints.py -q
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_guard.py -q
../../.venv/bin/python scripts/check_auth_entrypoints.py --check
git diff --check
git add hermes_cli/client_auth/entrypoints.json scripts/check_auth_entrypoints.py tests/hermes_cli/client_auth/test_entrypoints.py
# Then stage every production entry file changed to satisfy the scanner,
# using its explicit path; do not use a partial sample list or broad git add.
git commit -m "test: require auth policy for every entrypoint"
```

### Task 8: Enforce shared authorization at execution boundaries

- [ ] **Step 1: Add boundary behavior tests**

Create `tests/hermes_cli/client_auth/test_boundaries.py` and invoke real boundary functions with authenticated, locked, expired, and stale-scope consumers. Cover Agent turns, concurrent/sequential tool calls, gateway HTTP and WS messages, cron tick/job start, web-server requests, ACP requests, worker/delegate start, terminal spawn, file writes, git/network/message side effects. Exercise direct `model_tools.handle_function_call` callers, the standalone `mcp_serve.py` request surface, and `agent/transports/hermes_tools_mcp_server.py`. Each locked case must fail before SessionDB/history access, tool lookup, or the mocked irreversible operation is called.

- [ ] **Step 2: Insert the single primitive at central dispatch points**

Use only:

```python
from hermes_cli.client_auth.runtime import require_authorized

scope = require_authorized("tool.execute", expected=request_scope)
```

Insert it at `model_tools.handle_function_call` before any lookup or dispatch, because `agent/transports/hermes_tools_mcp_server.py`, `tools/tool_search.py`, `tools/code_execution_tool.py`, `agent/conversation_loop.py`, `run_agent.py`, and `agent/tool_executor.py` can reach this shared chokepoint through different paths. Keep defense-in-depth checks at `agent/tool_executor.py` before sequential/concurrent dispatch and `agent/agent_runtime_helpers.py::invoke_tool`. Add per-request checks to standalone `mcp_serve.py` and `agent/transports/hermes_tools_mcp_server.py`, before SessionDB/history or tool work, so a long-lived MCP process locks on owner revocation.

Also guard `gateway/platforms/api_server.py` before HTTP/WS handling and each WS message, `cron/scheduler.py` before tick and job execution, `hermes_cli/web_server.py` before protected routes, and `acp_adapter/server.py` before request/session work. Pass `AuthScope` into worker/delegate creation and reject stale scopes in the child before any Agent/tool import.

- [ ] **Step 3: Verify lock propagation and in-flight stopping**

Tests must prove owner EOF/epoch change blocks the next tool/message/job boundary, closes backend WS connections, and does not attempt to roll back already completed external effects.

- [ ] **Step 4: Run focused suites and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_boundaries.py tests/test_model_tools.py tests/test_mcp_serve.py tests/agent/transports/test_hermes_tools_mcp_server.py tests/integration/test_batch_runner.py tests/test_mini_swe_runner.py tests/agent/test_tool_executor_checkpoint_paths.py tests/run_agent/test_tool_executor_contextvar_propagation.py tests/gateway/test_api_server.py tests/cron/test_scheduler.py tests/acp/test_server.py -q
git diff --check
git add model_tools.py mcp_serve.py agent/tool_executor.py agent/agent_runtime_helpers.py agent/transports/hermes_tools_mcp_server.py gateway/platforms/api_server.py cron/scheduler.py hermes_cli/web_server.py acp_adapter/server.py tests/hermes_cli/client_auth/test_boundaries.py tests/test_model_tools.py tests/test_mcp_serve.py tests/agent/transports/test_hermes_tools_mcp_server.py tests/integration/test_batch_runner.py tests/test_mini_swe_runner.py
git commit -m "feat: enforce auth at shared execution boundaries"
```

Expected: new auth tests and the nearest existing boundary suites pass; no production test asserts implementation by reading source text.

Checkpoint B is intentionally fail-closed but not production-releasable on its own: background services may exit `20` until Plan 4 adds healthy `locked-waiting`. Do not publish Checkpoint B or C as a release; only the composed Plan 4 release gate is shippable.
