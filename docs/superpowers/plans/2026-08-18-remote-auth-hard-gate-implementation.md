# Hermes Remote Auth Hard Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require the fixed `https://c2sml.cn/agent` Django account before any official Hermes capability can start or execute, while preserving Desktop, CLI, Ink TUI, SSH, headless, service, and Docker operation.

**Architecture:** The server keeps Django username/password, CSRF, and Session Cookie authentication and adds one strict JSON session-status endpoint with non-sliding absolute expiry. Hermes adds one Python auth runtime protocol with `VaultOwner` and `MemoryOwner`, then routes every CLI, backend, Desktop IPC, tool, worker, service, SSH, and container boundary through `require_authorized()`; Desktop uses a Python JSONL bridge and connection-scoped authorization.

**Tech Stack:** Python 3.11+, Django 5.2, httpx, keyring 25.7, Unix sockets, Windows Named Pipes/pywin32, pytest, Electron, TypeScript, React, Vitest, Ink, s6-overlay, Podman/Docker.

---

## Program structure

The approved specification spans four independently reviewable delivery plans. Execute them in order because each later plan consumes contracts proven by the previous one.

1. [Server Session Contract](./2026-08-18-remote-auth-hard-gate-01-server-contract.md)
   - Version the currently unversioned `/opt/agent-history-portal` source.
   - Add `/agent/api/session/`, absolute Session expiry, public account-route 404 tests, and deployment rollback evidence.
2. [Core Runtime and CLI](./2026-08-18-remote-auth-hard-gate-02-core-runtime-cli.md)
   - Add the four Python auth modules, owner protocol, vault/memory storage, exact argv gate, provider-command migration, entrypoint manifest/scanner, and shared execution boundaries.
3. [Desktop, TUI, and SSH](./2026-08-18-remote-auth-hard-gate-03-desktop-tui-ssh.md)
   - Add the Desktop login shell, one IPC policy table, backend token scope, Ink login RPC, strict SSH host-key approval, and remote MemoryOwner flow.
4. [Background, Docker, and Release](./2026-08-18-remote-auth-hard-gate-04-background-docker-release.md)
   - Gate gateway/serve/cron/MCP/ACP/workers, add `locked-waiting`, integrate the auth runtime with existing s6, and run the complete cross-platform release matrix.

## Specification coverage

| Approved specification area | Owning plan |
|---|---|
| Fixed Django Session contract, independent memory authorization, administrator-only account lifecycle | Plan 1 |
| Exact CLI whitelist, shared owner/runtime, vault/memory modes, native IPC, entry discovery, execution boundaries | Plan 2 |
| Desktop bootstrap, default-deny IPC, HTTP/WS scope tokens, Ink login, connection isolation, strict SSH | Plan 3 |
| Noninteractive `locked-waiting`, host services, s6/Docker lifecycle, native CI, full release evidence | Plan 4 |

The final Plan 4 acceptance gate composes all four plans. A phase is not complete merely because its own unit tests pass; it must also preserve administrator-only account distribution, the five-shape unauthenticated CLI whitelist, and zero capability activity before authentication. Checkpoints B and C are development-only fail-closed states: some services may exit `20` until Plan 4 adds `locked-waiting`, so neither checkpoint may be shipped independently.

## Locked cross-plan interfaces

All plans use these names and schemas; an implementation task must not invent an alternate type.

```python
from dataclasses import dataclass
from enum import StrEnum

class AuthState(StrEnum):
    CHECKING = "checking"
    AUTHENTICATED = "authenticated"
    SIGNED_OUT = "signed_out"
    LOCKED = "locked"

@dataclass(frozen=True)
class AuthScope:
    runtime_instance_id: str
    epoch: int

@dataclass(frozen=True)
class ConnectionScope:
    connection_id: str
    auth: AuthScope

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

    @property
    def scope(self) -> AuthScope:
        return AuthScope(self.runtime_instance_id, self.epoch)
```

The Desktop and Ink wire projection is named `BridgeStatus` and contains exactly `state`, `username`, `runtime_instance_id`, `epoch`, `valid_until`, `session_expires_at`, and `reason`. It is a secret-free serialization of `RuntimeSnapshot`, not another state machine; `boot_id` stays inside the Python owner/consumer runtime.

The Django endpoint response is exactly:

```json
{"authenticated":true,"username":"alice","server_time":"2026-08-18T12:00:00+00:00","session_expires_at":"2026-09-01T12:00:00+00:00"}
```

or:

```json
{"authenticated":false}
```

The Desktop/Broker JSONL verbs are exactly `status`, `login`, and `logout`. The server origin, Cookie names, and Cookie path are constants, never configuration:

```python
AUTH_ORIGIN = "https://c2sml.cn"
AUTH_PREFIX = "/agent"
SESSION_COOKIE = "agent_history_sessionid"
CSRF_COOKIE = "agent_history_csrftoken"
```

## Global red-green and commit discipline

For every numbered task in the four plans:

1. For behavior not already guaranteed, add the named failing behavior test. For an existing security invariant, add a characterization test and record its green baseline before changing adjacent code.
2. Run only that test and capture the expected failure or characterization baseline.
3. Add the minimum production behavior.
4. Re-run the targeted test and its nearest existing regression file.
5. Run `git diff --check`.
6. Commit only that task with the message shown in its plan.

Python test suites must always run through the repository wrapper:

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh <test-path> -q
```

Desktop tests run from `apps/desktop`:

```bash
npx vitest run <test-path>
```

The final release gate is not satisfied by source-text assertions. It must start every discovered production entrypoint against a locked runtime and observe exit `20`, `AUTH_REQUIRED`, or an auth-only shell with zero capability process/network activity.

## Integration checkpoints

- Checkpoint A: server endpoint deployed and verified without changing existing history/memory behavior.
- Checkpoint B: CLI and direct Python entrypoints fail closed; only the five exact unauthenticated argv shapes work.
- Checkpoint C: Desktop and Ink render login before backend/Agent startup; local and remote scopes do not authorize each other.
- Checkpoint D: gateway/serve/cron/MCP/ACP/workers and Docker stay `locked-waiting` until authenticated.
- Checkpoint E: native macOS, Linux, and Windows runners plus real Docker integration pass; the worktree is clean and no secret appears in logs, argv, env, files, Renderer, or crash artifacts.
