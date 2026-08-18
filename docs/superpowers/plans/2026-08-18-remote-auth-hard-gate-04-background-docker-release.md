# Hermes Remote Auth Background, Docker, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every noninteractive Hermes service and container capability locked until the shared runtime is authenticated, stop new work immediately on revocation, and prove the full release has no unauthenticated bypass or credential leak.

**Architecture:** Noninteractive entrypoints connect to the same OS-user runtime but never collect a password. Installed services and containers treat `locked-waiting` as a healthy steady state: the auth owner remains alive while Agent, gateway, serve, cron, MCP, ACP, worker, delegate, kanban, dashboard, and profile services remain down or reject work. Docker reuses its current s6-overlay tree; the auth runtime receives only fixed up/down control over predeclared capability slots, and every slot retains its own `require_authorized()` backstop.

**Tech Stack:** Python 3.11+, pytest, systemd, launchd, Windows Scheduled Tasks, s6-overlay, POSIX shell, Docker/Podman integration tests, Electron/Ink release suites.

---

## File map

- Create: `tests/hermes_cli/client_auth/test_background_modes.py`
- Create: `tests/hermes_cli/client_auth/test_service_locked_waiting.py`
- Create: `tests/docker/test_auth_hard_gate.py`
- Create: `docker/s6-rc.d/hermes-auth-runtime/type`
- Create: `docker/s6-rc.d/hermes-auth-runtime/run`
- Create: `docker/s6-rc.d/hermes-auth-runtime/finish`
- Create: `docker/s6-rc.d/user/contents.d/hermes-auth-runtime`
- Create: `docker/s6-rc.d/dashboard/dependencies.d/hermes-auth-runtime`
- Create: `docker/s6-rc.d/main-hermes/dependencies.d/hermes-auth-runtime`
- Create: `scripts/check_auth_native_artifacts.py`
- Modify/extend: `docs/security/remote-auth-release-evidence.md`
- Modify: `hermes_cli/client_auth/runtime.py`, `hermes_cli/gateway.py`, `hermes_cli/web_server.py`, `hermes_cli/cron.py`, `hermes_cli/kanban.py`, `hermes_cli/mcp_startup.py`
- Modify: `gateway/run.py`, `gateway/platforms/api_server.py`, `cron/scheduler.py`, `acp_adapter/entry.py`, `acp_adapter/server.py`, `run_agent.py`
- Verify shared MCP/tool boundaries from Plan 2: `model_tools.py`, `mcp_serve.py`, `agent/transports/hermes_tools_mcp_server.py`
- Modify: `hermes_cli/service_manager.py`, `scripts/hermes-gateway`, `scripts/install.sh`, `scripts/install.ps1`
- Modify: `hermes_cli/container_boot.py`, `docker/entrypoint-dispatch.sh`, `docker/entrypoint.sh`, `docker/stage2-hook.sh`, `docker/s6-rc.d/dashboard/run`, `docker/s6-rc.d/main-hermes/run`
- Modify tests: `tests/hermes_cli/test_container_boot.py`, `tests/docker/test_s6_profile_gateway_integration.py`, `tests/gateway/test_api_server.py`, `tests/cron/test_scheduler.py`, `tests/acp/test_entry.py`
- Verify boundary tests: `tests/test_model_tools.py`, `tests/test_mcp_serve.py`, `tests/agent/transports/test_hermes_tools_mcp_server.py`
- Modify CI: `.github/workflows/tests.yml`, `.github/workflows/tests-os.yml`, `.github/workflows/docker.yml`

### Task 1: Define noninteractive `locked-waiting` behavior

- [ ] **Step 1: Write failing process-level tests for every background family**

Create `tests/hermes_cli/client_auth/test_background_modes.py`. Launch the real entry function for gateway run, serve, cron scheduler, MCP server, ACP, worker/delegate, kanban dispatcher, and `hermes-agent` with a locked runtime and non-TTY stdin. Assert each process:

- never calls `input`, `getpass`, or reads stdin;
- never imports/builds an Agent, opens SessionDB/history, starts MCP tools, binds a capability port, or invokes an external tool;
- either returns protocol `AUTH_REQUIRED`/exit `20`, or enters a bounded observable `locked-waiting` loop;
- emits only state, reason code, and “run `hermes login`” guidance.

The test uses injectable real boundaries and subprocess event capture; it must not inspect production source text.

Reuse the production locked-start harness created in Plan 2's `tests/hermes_cli/client_auth/test_entrypoints.py`; extend its fixtures/assertions for resident `locked-waiting` services instead of creating another entrypoint harness or another authority list.

- [ ] **Step 2: Run and record the first unauthorized side effect**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_background_modes.py -q
```

Expected: FAIL because current background entrypoints initialize capability code without the new runtime lease.

- [ ] **Step 3: Add one runtime wait primitive**

Extend `hermes_cli/client_auth/runtime.py` with:

```python
class LockedWaitingResult(StrEnum):
    AUTHENTICATED = "authenticated"
    OWNER_STOPPED = "owner_stopped"

def wait_until_authorized(
    boundary: str,
    *,
    stop_event: threading.Event,
    on_state: Callable[[RuntimeSnapshot], None],
) -> LockedWaitingResult:
    consumer = connect_runtime(noninteractive=True)
    while not stop_event.is_set():
        snapshot = consumer.snapshot()
        on_state(snapshot)
        if snapshot.state is AuthState.AUTHENTICATED:
            consumer.require_authorized(boundary, expected=snapshot.scope)
            return LockedWaitingResult.AUTHENTICATED
        consumer.wait_for_change(stop_event)
    return LockedWaitingResult.OWNER_STOPPED
```

`RuntimeSnapshot` contains no secrets. Unauthenticated consumer connections do not count toward MemoryOwner idle activity. Missing owner returns `runtime_unavailable`; noninteractive capability callers never start an owner or prompt. systemd, launchd, Scheduled Task, and s6 definitions start the fixed owner as a separate ordered service.

- [ ] **Step 4: Gate each background lifecycle before capability imports**

Use `wait_until_authorized` only in supervisors that intentionally remain resident. ACP/MCP request-mode entrypoints return exit `20`/protocol `AUTH_REQUIRED` immediately. Gateway, serve, and installed cron services may remain `locked-waiting`, then connect their own exclusive liveness consumer before importing capability modules. Worker/delegate/kanban children receive the parent `AuthScope`, establish their own connection, compare exact scope, and reject before imports when stale.

- [ ] **Step 5: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_background_modes.py tests/acp/test_entry.py tests/cron/test_scheduler.py tests/gateway/test_api_server.py -q
git diff --check
git add hermes_cli/client_auth/runtime.py hermes_cli/gateway.py hermes_cli/web_server.py hermes_cli/cron.py hermes_cli/kanban.py hermes_cli/mcp_startup.py gateway/run.py gateway/platforms/api_server.py cron/scheduler.py acp_adapter/entry.py acp_adapter/server.py run_agent.py tests/hermes_cli/client_auth/test_background_modes.py tests/acp/test_entry.py tests/cron/test_scheduler.py tests/gateway/test_api_server.py
git commit -m "feat: lock background entrypoints until login"
```

### Task 2: Stop work and remain healthy when authorization is revoked

- [ ] **Step 1: Write failing service-state tests**

Create `tests/hermes_cli/client_auth/test_service_locked_waiting.py`. Use a fake owner clock plus real service lifecycle adapters to prove:

- owner EOF, lease deadline, exact epoch mismatch, `runtime_instance_id` replacement, Session absolute expiry, TLS/network/5xx/429/schema failure all transition to `locked` without grace;
- gateway stops accepting new HTTP/WS/messages and closes WS connections;
- cron skips the current tick before claiming a job and records one redacted `AUTH_REQUIRED` event without retrying the same fire in a loop;
- MCP/ACP reject new requests;
- worker/delegate/kanban stop at their next boundary;
- service process remains healthy in `locked-waiting` and does not increment restart-loop/fatal-config counters;
- a later interactive login allows a fresh capability generation, while old tokens/scopes remain invalid.

- [ ] **Step 2: Run and verify current restart semantics fail**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_service_locked_waiting.py -q
```

- [ ] **Step 3: Implement lock propagation and service health signaling**

At owner change, increment epoch before cleanup, atomically mark the consumer locked, close listeners/WS acceptance, and drain only already-entered work until its next protected boundary. Add `auth_state="locked-waiting"` to existing status payloads and systemd notification without treating it as READY for capability work. Do not persist this transient state as desired “stopped”; the user’s desired service intent remains separate.

Cron advances or records the scheduled fire according to its existing exactly-once ledger but never executes it unauthenticated and never busy-retries. A subsequent future tick requires a new authorized scope.

- [ ] **Step 4: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_service_locked_waiting.py tests/test_model_tools.py tests/test_mcp_serve.py tests/agent/transports/test_hermes_tools_mcp_server.py tests/gateway/test_systemd_notify.py tests/gateway/test_systemd_watchdog_lifecycle.py tests/cron/test_scheduler.py tests/cron/test_execution_ledger.py -q
git diff --check
git add hermes_cli/client_auth/runtime.py gateway/run.py gateway/platforms/api_server.py gateway/systemd_notify.py cron/scheduler.py acp_adapter/server.py tests/hermes_cli/client_auth/test_service_locked_waiting.py tests/test_model_tools.py tests/test_mcp_serve.py tests/agent/transports/test_hermes_tools_mcp_server.py tests/gateway/test_systemd_notify.py tests/gateway/test_systemd_watchdog_lifecycle.py tests/cron/test_scheduler.py tests/cron/test_execution_ledger.py
git commit -m "feat: propagate auth revocation to services"
```

### Task 3: Make installed host services start only the owner and wait safely

- [ ] **Step 1: Write failing unit-generation tests**

Extend the existing systemd/launchd tests and add Windows scheduled-task fixtures. Assert generated services start a noninteractive auth runtime wrapper first, then a capability command that enters `locked-waiting`. They must never embed username, password, Cookie, CSRF, bearer token, or vault record in unit/plist/XML, argv, or environment. `Restart=on-failure`/launchd KeepAlive/Scheduled Task retry must not loop while signed out because locked wait exits neither fatally nor repeatedly.

- [ ] **Step 2: Run the current service tests**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_systemd_optional_directives.py tests/hermes_cli/test_systemd_watchdog_unit.py tests/hermes_cli/client_auth/test_service_locked_waiting.py -q
```

- [ ] **Step 3: Update service generation and installation**

In `hermes_cli/service_manager.py` and `scripts/hermes-gateway`, generate a runtime-owner unit/launch agent for the current OS user and make the gateway unit order after it. Windows installation creates a current-user owner task and a capability task that connects noninteractively. `scripts/install.sh` and `scripts/install.ps1` may offer service installation only after the install command itself passes the central CLI gate; they must not auto-enable linger, lower host security policy, or persist a Session.

On boot without login, owner starts with no secret, capability service reports `locked-waiting`, and no Agent/backend work starts. An interactive `hermes login` connects to that owner and unlocks the existing service generation.

- [ ] **Step 4: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_service_locked_waiting.py tests/hermes_cli/test_systemd_optional_directives.py tests/hermes_cli/test_systemd_watchdog_unit.py -q
git diff --check
git add hermes_cli/service_manager.py scripts/hermes-gateway scripts/install.sh scripts/install.ps1 tests/hermes_cli/client_auth/test_service_locked_waiting.py tests/hermes_cli/test_systemd_optional_directives.py tests/hermes_cli/test_systemd_watchdog_unit.py
git commit -m "feat: make host services auth aware"
```

### Task 4: Preserve Docker desired intent but register every capability down

- [ ] **Step 1: Change container reconciliation tests to the new invariant**

Update `tests/hermes_cli/test_container_boot.py`. For default and named profiles whose prior `gateway_state.json` is `running`, call the real keyword-only API and assert:

```python
actions = reconcile_profile_gateways(
    hermes_home=hermes_home,
    scandir=scandir,
    dry_run=False,
    container_argv=["/init"],
)
coder = next(action for action in actions if action.profile == "coder")
assert coder.prior_state == "running"
assert coder.action == "registered"
assert (scandir / "gateway-coder" / "down").exists()
```

Add equivalent cases for dashboard, serve, cron, and main Hermes static services. A restart must retain desired intent in existing state files but never translate it into an immediate process spawn.

- [ ] **Step 2: Run and capture the existing autostart failure**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_container_boot.py -q
```

Expected: FAIL because current reconciliation removes `down` for prior running gateways.

- [ ] **Step 3: Implement down-only reconciliation**

Change `container_boot.py` so every call to the existing registration helper uses `start=False`. Preserve the real `list[ReconcileAction]` API and its fields; prior desired state remains in `prior_state`, while a registered-down slot reports `action="registered"`. Do not invent `desired`/`started` fields and do not overwrite `gateway_state.json` merely because auth is locked. Modify static service definitions and `stage2-hook.sh` so dashboard, main Hermes, serve, and cron capability slots begin down as well. `entrypoint-dispatch.sh` must enter the s6 auth-runtime topology; it must not exec an ordinary Hermes CLI first.

- [ ] **Step 4: Keep independent entry guards**

Every s6 run script calls the noninteractive runtime preflight before execing its capability. A mistakenly removed `down` marker must therefore yield `AUTH_REQUIRED` without binding ports, importing Agent, or starting jobs.

- [ ] **Step 5: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_container_boot.py tests/docker/test_gateway_bootstrap_state.py tests/docker/test_profile_gateway.py -q
git diff --check
git add hermes_cli/container_boot.py docker/entrypoint-dispatch.sh docker/entrypoint.sh docker/stage2-hook.sh docker/s6-rc.d/dashboard/run docker/s6-rc.d/main-hermes/run tests/hermes_cli/test_container_boot.py tests/docker/test_gateway_bootstrap_state.py tests/docker/test_profile_gateway.py
git commit -m "feat: keep docker capabilities down before login"
```

### Task 5: Integrate MemoryOwner into the existing s6 tree with least privilege

- [ ] **Step 1: Write failing real-s6 permission tests**

Extend `tests/docker/test_s6_profile_gateway_integration.py` and create `tests/docker/test_auth_hard_gate.py`. In a real built container assert:

- only `hermes-auth-runtime` is up immediately after boot;
- auth runtime runs as the fixed non-root `hermes` UID and its runtime directory is tmpfs-backed, `0700`, and outside persistent `HERMES_HOME`;
- service definitions remain root-owned and non-writable by `hermes`;
- precreated `supervise/control` FIFOs permit the Hermes UID to send only s6 up/down signals to the fixed capability slots;
- Hermes UID cannot add/replace a run script, access Docker socket, execute a root helper, or control an arbitrary service;
- Cookie and CSRF sentinels have zero matches in mounted volumes, image history, container config/env, logs, `/proc/*/cmdline`, and `/proc/*/environ`.

- [ ] **Step 2: Add the auth runtime s6 service**

Create `docker/s6-rc.d/hermes-auth-runtime` as a longrun that executes the existing `runtime.py` MemoryOwner entry under `s6-setuidgid hermes`. Its finish script leaves capability services down and reports `locked-waiting`; it must not cause a restart storm. Add it to the existing `user` bundle and as an ordering dependency for static capability services, but retain their `down` markers.

- [ ] **Step 3: Add a fixed s6 lifecycle adapter inside `runtime.py`**

The adapter receives a compile-time set of service names produced by container reconciliation. On an authenticated transition, it brings up only services whose separate desired-intent record is `running`. On lock/logout/owner failure, it sends down to every capability slot before publishing the locked transition. It never accepts service names over Broker IPC, never edits definitions, never calls a shell, and never controls root services.

- [ ] **Step 4: Run host-side and real-container tests**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_container_boot.py tests/docker/test_s6_profile_gateway_integration.py tests/docker/test_auth_hard_gate.py -q
```

The Docker fixtures select Docker or Podman through the repository’s existing fixture. If no engine is available locally, host-side tests must pass and the real-container tests must report a CI-required skip marker rather than silently pass.

- [ ] **Step 5: Commit**

```bash
git diff --check
git add docker/s6-rc.d/hermes-auth-runtime/type docker/s6-rc.d/hermes-auth-runtime/run docker/s6-rc.d/hermes-auth-runtime/finish docker/s6-rc.d/user/contents.d/hermes-auth-runtime docker/s6-rc.d/dashboard/dependencies.d/hermes-auth-runtime docker/s6-rc.d/main-hermes/dependencies.d/hermes-auth-runtime docker/stage2-hook.sh hermes_cli/client_auth/runtime.py hermes_cli/service_manager.py tests/docker/test_s6_profile_gateway_integration.py tests/docker/test_auth_hard_gate.py
git commit -m "feat: supervise docker auth runtime with s6"
```

### Task 6: Prove Docker login, unlock, revocation, and reboot behavior

- [ ] **Step 1: Add the four-state container integration scenario**

In `tests/docker/test_auth_hard_gate.py`, drive a fake fixed-origin Django server through the same `AuthClient` contract and a real container:

1. **fresh boot:** only auth runtime up; all capability spawn and port counts are zero;
2. **interactive login:** `docker exec -it` presents the normal terminal login prompt, sends username/password through the attached stdin/TTY, owner authenticates, and only desired services become up;
3. **server revocation:** the next periodic validation increments epoch, closes listeners, brings capability services down, and retains auth runtime in `locked-waiting`;
4. **container restart:** no Cookie survives; only auth runtime is up and a new login is required.

Also force s6 to bring up a capability while locked and assert its own entry guard rejects with exit `20` before capability activity.

- [ ] **Step 2: Run the integration test and inspect the process tree**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/docker/test_auth_hard_gate.py tests/docker/test_container_restart.py tests/docker/test_s6_profile_gateway_integration.py -q
```

Expected: all four phases pass using the real s6 binaries. Capture process-tree and port-list evidence as test artifacts.

- [ ] **Step 3: Run image and compose regression tests**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/docker -q
```

- [ ] **Step 4: Commit integration coverage**

```bash
git diff --check
git add tests/docker/test_auth_hard_gate.py tests/docker/test_container_restart.py tests/docker/test_s6_profile_gateway_integration.py
git commit -m "test: verify docker auth lifecycle"
```

### Task 7: Add native transport and service CI coverage

- [ ] **Step 1: Create only the native-artifact schema validator**

Create `scripts/check_auth_native_artifacts.py`. It has one responsibility: validate that all native jobs supplied a JSON artifact with these keys and accepted values:

```json
{
  "platform": "linux|macos|windows",
  "owner_transport": "unix-peercred|unix-getpeereid|named-pipe-sid",
  "locked_start_passed": true,
  "handle_noninheritance_passed": true,
  "service_locked_waiting_passed": true
}
```

It fails if a platform is missing, a field/schema/value is invalid, or duplicate platform artifacts disagree. It does not run tests, scan entrypoints, verify help, or reimplement locked-start assertions. CI invokes the existing entrypoint scanner, static-help checker, and Plan 2 production locked-start tests directly, leaving one authority for each invariant.

- [ ] **Step 2: Define the native matrix in the existing CI workflow**

Add jobs on actual Linux, macOS, and Windows runners:

- Linux: filesystem Unix socket, `0700/0600`, `SO_PEERCRED`, no abstract namespace, `PR_SET_DUMPABLE=0`, core limit, fd `CLOEXEC`, systemd unit generation;
- macOS: `_CS_DARWIN_USER_TEMP_DIR`, `getpeereid`, `PT_DENY_ATTACH`, core limit, fd `CLOEXEC`, launchd generation;
- Windows: SID-derived Named Pipe, first-instance flag, DACL, impersonation/token SID, handle-list noninheritance, WER/mitigation calls, Scheduled Task generation.

Each job runs the same owner parity, lease, entry, background, and secret-artifact tests. No test monkeypatches the platform identity.

Each native job writes the artifact from tests running on that actual runner. The aggregator validates artifact-declared platform against CI-provided runner metadata; it does not infer or fake another `sys.platform`.

- [ ] **Step 3: Add the Docker job**

Build the release image, run `tests/docker/test_auth_hard_gate.py`, archive process/port/redaction artifacts, and require it for merge. The job must run with a non-root Hermes UID and real s6-overlay.

- [ ] **Step 4: Run the local composition check and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth -q
../../.venv/bin/python scripts/check_auth_entrypoints.py --check
../../.venv/bin/python scripts/generate_auth_free_help.py --check
../../.venv/bin/python scripts/check_auth_native_artifacts.py --allow-partial-local .auth-artifacts
git diff --check
```

Stage the exact workflow files changed plus the release checker and commit:

```bash
git add scripts/check_auth_native_artifacts.py .github/workflows/tests.yml .github/workflows/tests-os.yml .github/workflows/docker.yml
git commit -m "ci: require native auth hard gate matrix"
```

### Task 8: Run the full release and server-integration acceptance gate

- [ ] **Step 1: Verify the server contract and independent memory authorization**

Against staging at the fixed origin, run Plan 1 smoke tests with a temporary admin-issued account. Verify Session login/status/logout, non-sliding absolute expiry, Cookie rotation, axes behavior, and memory API `401` without Session. Verify public signup, invitation, password reset/change, and account-create paths return `404`. Delete or disable the temporary account through Django Admin after the test; do not add any client lifecycle endpoint.

- [ ] **Step 2: Run repository Python suites through the required wrapper**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth tests/tui_gateway tests/acp tests/acp_adapter tests/cron tests/gateway tests/docker -q
```

- [ ] **Step 3: Run Desktop and TUI suites**

```bash
cd apps/desktop
npm run check
npm run test:e2e -- e2e/auth-hard-gate.spec.ts
cd ../../ui-tui
npm run check
```

- [ ] **Step 4: Run release entry and secret scans**

```bash
cd ..
../../.venv/bin/python scripts/check_auth_entrypoints.py --check
../../.venv/bin/python scripts/generate_auth_free_help.py --check
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_entrypoints.py -q
../../.venv/bin/python scripts/check_auth_native_artifacts.py .auth-artifacts
git diff --check
git status --short
```

The generated-artifact scan uses unique sentinel credentials and must report zero secret matches across argv, env, files, logs, Renderer payloads, crash artifacts, image config, and mounted volumes. Source-code occurrences in test fixture definitions are excluded by path and are not counted as runtime leaks.

- [ ] **Step 5: Confirm all acceptance invariants**

Record evidence that:

- exactly the five raw unauthenticated CLI shapes work;
- every discovered capability entry fails closed;
- Desktop and Ink show login before capability startup;
- local and each SSH target use independent scopes;
- headless login works through stdin and never persists Session;
- services and Docker remain healthy in `locked-waiting`;
- revocation stops new side effects at the next boundary;
- restart invalidates MemoryOwner state;
- local vault login survives app restart but still validates online;
- administrator provisioning is the only account distribution path;
- changing the memory framework does not affect the Session contract.

- [ ] **Step 6: Record and commit the redacted release evidence**

Create `docs/security/remote-auth-release-evidence.md` with the server commit/image identifiers, Hermes commit, exact verification commands, pass/fail totals, native matrix artifact digests, Docker process/port assertions, and the result of every acceptance invariant above. Refer to the administrator-issued test account by a non-identifying label only. Do not record its username, password, Cookie, CSRF value, bearer token, raw Django response, vault metadata, or runtime endpoint path.

```bash
git diff --check
git add docs/security/remote-auth-release-evidence.md
git commit -m "test: complete remote auth release gate"
```

Do not commit credentials, staging responses containing identifiers, local Keychain metadata, runtime sockets, container volumes, or raw CI secret-scan artifacts.
