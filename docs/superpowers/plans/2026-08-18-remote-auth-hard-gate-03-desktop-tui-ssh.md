# Hermes Remote Auth Desktop, TUI, and SSH Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Desktop and Ink present an authentication-only shell before any Hermes capability starts, isolate authorization by execution connection, and support password login over strictly verified SSH without persisting remote credentials.

**Architecture:** Electron owns one connection-scoped auth coordinator backed by the closed Python JSONL bridge from Plan 2. A single default-deny IPC policy maps every channel to `auth-free`, `local`, `connection`, or `both`; backend bearer tokens are random short-lived secrets bound to `connection_id + runtime_instance_id + epoch`. Ink reuses `tui_gateway` for the same three auth verbs. SSH first completes explicit host-key verification, then starts only the remote auth bridge and MemoryOwner; the remote Hermes backend starts only after that owner authenticates.

**Tech Stack:** Electron 40, TypeScript 6, React 19, Vitest, Playwright, Ink, Node child processes, OpenSSH, Python JSONL bridge, pytest.

---

## File map

- Create: `apps/desktop/electron/auth-bridge.ts`
- Create: `apps/desktop/electron/auth-coordinator.ts`
- Create: `apps/desktop/electron/auth-scope-token.ts`
- Create: `apps/desktop/electron/guarded-ipc.ts`
- Create: `apps/desktop/electron/auth-bridge.test.ts`
- Create: `apps/desktop/electron/auth-coordinator.test.ts`
- Create: `apps/desktop/electron/auth-scope-token.test.ts`
- Create: `apps/desktop/electron/guarded-ipc.test.ts`
- Create: `apps/desktop/src/components/auth-gate.tsx`
- Create: `apps/desktop/src/components/auth-gate.test.tsx`
- Create: `apps/desktop/e2e/auth-hard-gate.spec.ts`
- Create: `ui-tui/src/authGate.tsx`
- Create: `ui-tui/src/__tests__/authGate.test.tsx`
- Create: `tests/tui_gateway/test_account_auth.py`
- Create/append initial sections: `docs/security/remote-auth-release-evidence.md`
- Modify: `apps/desktop/electron/main.ts`, `apps/desktop/electron/preload.ts`, `apps/desktop/electron/primary-backend-startup.ts`, `apps/desktop/electron/connection-registry.ts`, `apps/desktop/electron/profile-session-routing.ts`
- Modify: `apps/desktop/electron/ssh-config.ts`, `apps/desktop/electron/ssh-connection.ts`, `apps/desktop/electron/ssh-bootstrap-coordinator.ts`
- Modify: `apps/desktop/src/main.tsx`, `apps/desktop/src/app/index.tsx`
- Modify locales: `apps/desktop/src/i18n/en.ts`, `apps/desktop/src/i18n/ja.ts`, `apps/desktop/src/i18n/zh.ts`, `apps/desktop/src/i18n/zh-hant.ts`; verify `apps/desktop/src/i18n/ar.ts` uses the intended English fallback.
- Modify: `ui-tui/src/entry.tsx`, `ui-tui/src/app.tsx`, `ui-tui/src/gatewayClient.ts`, `tui_gateway/entry.py`, `tui_gateway/server.py`
- Modify backend enforcement: `hermes_cli/client_auth/runtime.py`, `hermes_cli/web_server.py`, `gateway/platforms/api_server.py`

### Task 1: Start Desktop with only the local auth bridge

- [x] **Step 1: Write a failing bridge lifecycle test**

Create `apps/desktop/electron/auth-bridge.test.ts`. Inject a fake child-process factory and assert that startup uses the packaged Python with `-m hermes_cli.client_auth.bridge`, `stdio: ['pipe', 'pipe', 'pipe']`, an empty auth-secret environment, and no Hermes backend command. Cover bounded request IDs, malformed JSON, child exit, timeout, and stderr redaction. The test must assert that neither password nor `agent_history_sessionid` reaches emitted diagnostics.

- [x] **Step 2: Run the focused test and capture the missing-module failure**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-bridge.test.ts
```

Expected: FAIL because `auth-bridge.ts` does not exist.

- [x] **Step 3: Implement the closed bridge client**

Create `apps/desktop/electron/auth-bridge.ts` with a `DesktopAuthBridge` class. It may issue only these request shapes:

```ts
type AuthMethod = 'status' | 'login' | 'logout'

type BridgeStatus = {
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
  runtime_instance_id: string
  epoch: number
  valid_until: number
  session_expires_at: string | null
  reason: string | null
}
```

Use newline-delimited JSON version `1`, a 64 KiB response-line cap, monotonically increasing request IDs, a 15-second timeout, and one pending-request map. Reject unknown fields and unknown methods before writing stdin. On child EOF, parse error, timeout, or schema drift, reject all pending calls with redacted `runtime_unavailable`. Do not copy child stderr into Renderer errors.

- [x] **Step 4: Wire bridge startup ahead of backend startup**

In `apps/desktop/electron/main.ts`, construct the bridge after the existing signed bootstrap/install shell is ready but before `runPrimaryBackendStartup`, deep-link dispatch, global shortcuts, terminal registration, HUD, Quick Entry, Pet Overlay, or capability windows. Update `primary-backend-startup.ts` to require an authenticated `ConnectionScope` input before either `ensureLocalRuntime` or `connectRemote` can be called.

- [x] **Step 5: Run and commit**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-bridge.test.ts electron/primary-backend-startup.test.ts
npm run typecheck
cd ../..
git diff --check
git add apps/desktop/electron/auth-bridge.ts apps/desktop/electron/auth-bridge.test.ts apps/desktop/electron/primary-backend-startup.ts apps/desktop/electron/primary-backend-startup.test.ts apps/desktop/electron/main.ts
git commit -m "feat: start desktop through auth bridge"
```

### Task 2: Replace direct Electron IPC registration with one default-deny policy

- [x] **Step 1: Write failing policy-table behavior tests**

Create `apps/desktop/electron/guarded-ipc.test.ts`. Use a fake `ipcMain` and real handler registration calls to assert:

```ts
type ChannelAuthPolicy = 'auth-free' | 'local' | 'connection' | 'both'

const requiredCases = [
  ['hermes:auth:status', 'auth-free'],
  ['hermes:auth:login', 'auth-free'],
  ['hermes:auth:logout', 'auth-free'],
  ['hermes:bootstrap:get', 'auth-free'],
  ['hermes:terminal:start', 'local'],
  ['hermes:fs:writeText', 'local'],
  ['hermes:gateway:ws-url-for', 'connection'],
  ['hermes:connection-config:apply', 'both']
] as const
```

The full test must enumerate all channels registered by production registration functions, compare that set to `Object.keys(CHANNEL_AUTH_POLICY)`, and invoke representative handlers under authenticated, locked, and stale-scope coordinators. An unclassified channel must throw before its handler runs. Do not read `main.ts` as text and do not assert a hard-coded handler count.

- [x] **Step 2: Run and verify default-deny behavior is absent**

```bash
cd apps/desktop
npx vitest run --project electron electron/guarded-ipc.test.ts
```

Expected: FAIL because handlers register directly through `ipcMain`.

- [x] **Step 3: Implement the single policy adapter**

Create `apps/desktop/electron/guarded-ipc.ts` with one exported `CHANNEL_AUTH_POLICY`, `guardedHandle`, and `guardedOn`. `guardedHandle` and `guardedOn` must determine the actual execution target from validated payload and sender ownership, ask `AuthCoordinator.require(policy, connectionId)`, then invoke the handler. Missing policy, missing connection ID, unknown sender, stale scope, and bridge failure all reject with a redacted `AUTH_REQUIRED` error. Derive any compatibility `AUTH_FREE_CHANNELS` set from `CHANNEL_AUTH_POLICY`; do not maintain a second list.

Auth-free policy is limited to:

- the three account-auth channels;
- window close/minimize and bootstrap `get`, `continue-local`, `repair`, `reset`, `cancel`;
- theme rendering required by the login shell;
- bounded, redacted renderer-error reporting.

Every channel that reads files, sessions, logs, config, clipboard, network state, secrets, profiles, or starts a process remains protected. `hermes:version` is not an Electron capability exception; the login shell displays its packaged build metadata without an IPC call.

- [x] **Step 4: Route every `ipcMain.handle/on` call through the adapter**

Refactor `main.ts` registration into callable registration functions and pass the adapter into them. Remove direct `ipcMain.handle/on` usage outside `guarded-ipc.ts`. Keep real behavior tests for channel authorization; add ESLint `no-restricted-imports` configuration scoped to Electron files so only `guarded-ipc.ts` may import `ipcMain` for handler registration.

- [x] **Step 5: Run and commit**

```bash
cd apps/desktop
npx vitest run --project electron electron/guarded-ipc.test.ts
npm run lint
npm run typecheck
cd ../..
git diff --check
git add apps/desktop/electron/guarded-ipc.ts apps/desktop/electron/guarded-ipc.test.ts apps/desktop/electron/main.ts apps/desktop/electron/preload.ts apps/desktop/eslint.config.mjs
git commit -m "feat: default deny desktop ipc"
```

### Task 3: Render the hard login gate and propagate lock events

- [x] **Step 1: Write failing Renderer and startup tests**

Create `apps/desktop/src/components/auth-gate.test.tsx` and `apps/desktop/e2e/auth-hard-gate.spec.ts`. Assert that a signed-out first launch renders only fixed server text, username, password, login, retry, and an administrator-contact message. Assert absence of registration, invitation, password reset/change, server URL, insecure TLS, offline, and skip controls. Instrument Electron startup and assert zero backend spawn/connect, WebSocket, terminal PTY, deep-link delivery, HUD, Quick Entry, and Pet Overlay activity before authentication.

- [x] **Step 2: Run and record the failure**

```bash
cd apps/desktop
npx vitest run --project ui src/components/auth-gate.test.tsx
npx vitest run --project electron electron/auth-coordinator.test.ts
```

Expected: FAIL because the auth gate and coordinator do not exist.

- [x] **Step 3: Implement `AuthCoordinator` and `AuthGate`**

Create `auth-coordinator.ts` with a map keyed by `connection_id`; `local` is a reserved ID. Each entry stores the full runtime scope, never a boolean. It subscribes to bridge/runtime status changes, invalidates the prior scope before logout, and emits `locked` before cleanup.

Create `auth-gate.tsx` and wrap `apps/desktop/src/app/index.tsx` from `main.tsx`. Password remains component-local only until `window.hermes.auth.login` resolves, then the input state is cleared. Renderer stores no password, Cookie, CSRF, or vault record. On lock, unmount protected app trees and ask the main process to close backend connections and capability windows before returning to the login shell.

- [x] **Step 4: Add complete locale messages**

Add the same auth message keys to `en`, `ja`, `zh`, and `zh-hant`. Map reason codes to user-safe text. All other locales fall back to English. Add a catalog test proving the required catalogs contain every key, explicitly load `ar.ts` and prove each missing Arabic auth key resolves to the English fallback, and prove no message suggests self-registration or self-service reset; the password-reset instruction must be “contact the server administrator”.

- [x] **Step 5: Run and commit**

```bash
cd apps/desktop
npx vitest run --project ui src/components/auth-gate.test.tsx src/i18n
npx vitest run --project electron electron/auth-coordinator.test.ts electron/main-window-lifecycle.test.ts
npm run typecheck
cd ../..
git diff --check
git add apps/desktop/electron/auth-coordinator.ts apps/desktop/electron/auth-coordinator.test.ts apps/desktop/electron/main.ts apps/desktop/src/main.tsx apps/desktop/src/app/index.tsx apps/desktop/src/components/auth-gate.tsx apps/desktop/src/components/auth-gate.test.tsx apps/desktop/src/i18n/en.ts apps/desktop/src/i18n/ja.ts apps/desktop/src/i18n/zh.ts apps/desktop/src/i18n/zh-hant.ts apps/desktop/e2e/auth-hard-gate.spec.ts
git commit -m "feat: add desktop account hard gate"
```

### Task 4: Bind backend HTTP and WebSocket access to unforgeable auth scope tokens

- [x] **Step 1: Write failing token and direct-connect tests**

Create `auth-scope-token.test.ts` and Python tests in `tests/hermes_cli/client_auth/test_runtime.py`. Assert that issued bearer tokens contain at least 256 bits of randomness, are stored hashed in the backend, expire within 60 seconds, and bind exact `connection_id`, `runtime_instance_id`, and `epoch`. Test HTTP request, WS upgrade, and every WS message. A copied tuple without the random bearer, a token from another connection, a token issued before logout, and a token issued by a previous owner instance must all fail before route work.

- [x] **Step 2: Run the focused tests and observe old-token acceptance**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-scope-token.test.ts
cd ../..
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py tests/gateway/test_api_server.py -q
```

- [x] **Step 3: Implement issuance and backend verification**

Create `auth-scope-token.ts`. Generate the bearer with `crypto.randomBytes(32)`, transmit it to the chosen backend over its already protected child stdin/control channel, and expose only the connection-specific bearer to the owning Renderer window. Extend `runtime.py` with token registration/revocation inside the existing runtime protocol; do not create a fifth auth module. `web_server.py` and `api_server.py` verify the bearer digest, exact tuple, connection ID, TTL, and current owner liveness at request start and per WS message.

On `locked`, `logout`, bridge EOF, or connection switch, revoke all tokens for that scope and close corresponding WS connections. Never put a token in a URL, log, crash report, command-line argument, persistent connection registry, or environment variable.

- [x] **Step 4: Prove Node and PTY children do not inherit auth liveness handles**

Add behavior cases around the real `spawn` and `node-pty` helpers in `main.ts`. After a parent connection is closed, a child process must not keep the runtime liveness endpoint open and its next protected request must fail. Pass explicit `stdio` arrays and close every unrelated fd/Windows handle.

- [x] **Step 5: Run and commit**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-scope-token.test.ts electron/backend-connection-state.test.ts electron/backend-ownership.test.ts
cd ../..
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py tests/gateway/test_api_server.py -q
git diff --check
git add apps/desktop/electron/auth-scope-token.ts apps/desktop/electron/auth-scope-token.test.ts apps/desktop/electron/main.ts hermes_cli/client_auth/runtime.py hermes_cli/web_server.py gateway/platforms/api_server.py tests/hermes_cli/client_auth/test_runtime.py tests/gateway/test_api_server.py
git commit -m "feat: bind desktop backends to auth scope"
```

### Task 5: Isolate local and remote connection scopes

- [x] **Step 1: Write the failing authorization matrix**

Extend `auth-coordinator.test.ts`, `connection-registry.test.ts`, and `profile-session-routing` tests with:

| local | remote A | remote B | operation | result |
|---|---|---|---|---|
| locked | authenticated | authenticated | local terminal/fs/git/clipboard | reject |
| locked | authenticated | authenticated | remote A backend request | allow A only |
| authenticated | locked | authenticated | remote A backend request | reject |
| authenticated | locked | authenticated | remote B background request | allow B only |
| authenticated | authenticated | authenticated | operation classified `both` for A | allow local + A only |

Also assert that switching the foreground from A to B revokes the foreground A token before B is displayed, while A background work can continue only with A's still-valid scope. Logout of A must not mutate local or B.

- [x] **Step 2: Implement connection-scoped routing**

Persist only non-secret connection configuration. Add `connection_id + runtime_instance_id + epoch` to in-memory backend ownership and routing records. Every protected IPC call resolves the actual execution target; it must not use a global “current authenticated” flag. Rehome and profile routing must request a fresh token for the destination scope.

- [x] **Step 3: Run and commit**

```bash
cd apps/desktop
npx vitest run --project electron electron/auth-coordinator.test.ts electron/connection-registry.test.ts electron/connection-apply.test.ts
npm run typecheck
cd ../..
git diff --check
git add apps/desktop/electron/auth-coordinator.ts apps/desktop/electron/auth-coordinator.test.ts apps/desktop/electron/connection-registry.ts apps/desktop/electron/connection-registry.test.ts apps/desktop/electron/profile-session-routing.ts apps/desktop/electron/profile-session-routing.test.ts apps/desktop/electron/connection-apply.ts apps/desktop/electron/connection-apply.test.ts
git commit -m "feat: isolate desktop auth by connection"
```

### Task 6: Add the Ink login shell over existing `tui_gateway` RPC

- [x] **Step 1: Write failing Python RPC tests**

Create `tests/tui_gateway/test_account_auth.py`. Start `tui_gateway.entry` in auth-shell mode with a locked owner and assert its initial frame is `auth.status`, not `gateway.ready`. Only `auth.status`, `auth.login`, and `auth.logout` are accepted before authentication. `session.*`, prompt submission, history, model, tools, MCP, and file-related RPC return code `20`/`AUTH_REQUIRED` without building an Agent or opening SessionDB.

- [x] **Step 2: Write failing Ink form tests**

Create `ui-tui/src/__tests__/authGate.test.tsx`. Render the form and assert username/password fields, hidden password input, submit/retry behavior, reason-code text, and zero registration/reset/server/offline controls. Assert the main `App` is mounted only after a matching authenticated scope arrives and unmounted immediately on a lock event.

- [x] **Step 3: Run the red tests**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/tui_gateway/test_account_auth.py -q
cd ui-tui
npx vitest run src/__tests__/authGate.test.tsx src/__tests__/gatewayClient.test.ts
```

Expected: FAIL because the gateway currently emits capability-ready state before account authentication.

- [x] **Step 4: Implement auth-only startup and the three RPC methods**

Move Agent/session/MCP imports and startup in `tui_gateway/entry.py` and `server.py` behind `require_authorized("tui.agent")`. Auth-shell startup may import only the guard/runtime transport needed for the three methods. Validate the same fixed request schemas as Desktop bridge and return no secrets. After authentication, publish `auth.changed`, then initialize the existing gateway exactly once. On owner EOF or epoch change, stop accepting prompts, tear down Agent/session workers at the next boundary, and return to auth shell.

Update `GatewayClient` with typed `authStatus`, `authLogin`, and `authLogout` methods. Render `AuthGate` from `entry.tsx` before importing/rendering the full `App`. Password is never included in lifecycle logs or crash breadcrumbs.

- [x] **Step 5: Run and commit**

```bash
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/tui_gateway/test_account_auth.py tests/hermes_cli/client_auth/test_guard.py -q
cd ui-tui
npx vitest run src/__tests__/authGate.test.tsx src/__tests__/gatewayClient.test.ts
npm run typecheck
cd ..
git diff --check
git add tui_gateway/entry.py tui_gateway/server.py tests/tui_gateway/test_account_auth.py ui-tui/src/authGate.tsx ui-tui/src/__tests__/authGate.test.tsx ui-tui/src/entry.tsx ui-tui/src/app.tsx ui-tui/src/gatewayClient.ts ui-tui/src/__tests__/gatewayClient.test.ts
git commit -m "feat: gate ink tui with account login"
```

### Task 7: Enforce explicit SSH host trust before remote password login

- [x] **Step 1: Write failing host-key policy tests**

Extend `ssh-config.test.ts`, `ssh-connection.test.ts`, and `ssh-bootstrap-coordinator.test.ts` with real command-construction behavior:

- known host uses `StrictHostKeyChecking=yes` and the normal user known-hosts files;
- changed key fails closed and never starts auth bridge;
- unknown host first runs `ssh-keyscan` through a bounded, non-shell spawn, computes the SHA256 fingerprint locally, and returns a confirmation request that clearly tells the user to verify the fingerprint through a trusted out-of-band source;
- only an explicit UI confirmation atomically appends the exact key to the user known-hosts file with mode `0600`, then retries with `StrictHostKeyChecking=yes`;
- cancel leaves known-hosts unchanged;
- no account password path contains `accept-new`, `StrictHostKeyChecking=no`, or an empty `UserKnownHostsFile`.

- [x] **Step 2: Run and capture the current `accept-new` failure**

```bash
cd apps/desktop
npx vitest run --project electron electron/ssh-config.test.ts electron/ssh-connection.test.ts electron/ssh-bootstrap-coordinator.test.ts
```

Expected: FAIL because `ssh-connection.ts` currently emits `StrictHostKeyChecking=accept-new`.

- [x] **Step 3: Implement explicit TOFU and strict reconnects**

Change `baseSshOptions` to `StrictHostKeyChecking=yes`. Add a typed `UnknownHostKey` result containing host, algorithm, and SHA256 fingerprint but no command stderr. Add an Electron confirmation dialog before any credential-bearing operation; its copy must state that `ssh-keyscan` is not proof of identity and require the user to compare the fingerprint with a trusted administrator/out-of-band source. Re-read the effective key immediately before append and reject if it differs from the displayed fingerprint. Use an owner-only temporary file in the same directory, `fsync`, rename, and mode `0600`; preserve existing known-host entries.

- [x] **Step 4: Implement the remote auth-only bootstrap**

In `ssh-bootstrap-coordinator.ts`, the ordered remote flow is:

1. verify/confirm host key;
2. open the ControlMaster under strict checking;
3. exec the packaged `python -m hermes_cli.client_auth.bridge` only; the private resolver first connects to any live owner for that remote OS user and, only if none exists, elects `MemoryOwner` from the SSH execution context;
4. request remote status;
5. if locked, send the bridge login JSON over child stdin;
6. wait until the detached MemoryOwner endpoint is ready and authenticated;
7. start the ordinary remote backend and tunnel with a new connection-scoped token.

The remote bridge detaches the broker from the login TTY and all inherited fds before reporting success. Account password appears only in the bounded stdin frame; add spies proving it is absent from argv, env, filesystem writes, logs, errors, connection registry, and backend command. Local vault records are never read or forwarded for a remote connection. Remote logout affects only that remote OS-user runtime.

- [x] **Step 5: Run SSH and Desktop integration tests, then commit**

```bash
cd apps/desktop
npx vitest run --project electron electron/ssh-config.test.ts electron/ssh-connection.test.ts electron/ssh-bootstrap-coordinator.test.ts electron/auth-coordinator.test.ts
npm run typecheck
cd ../..
git diff --check
git add apps/desktop/electron/ssh-config.ts apps/desktop/electron/ssh-config.test.ts apps/desktop/electron/ssh-connection.ts apps/desktop/electron/ssh-connection.test.ts apps/desktop/electron/ssh-bootstrap-coordinator.ts apps/desktop/electron/ssh-bootstrap-coordinator.test.ts apps/desktop/electron/auth-coordinator.ts apps/desktop/electron/main.ts hermes_cli/client_auth/bridge.py hermes_cli/client_auth/runtime.py
git commit -m "feat: authenticate remote backends over strict ssh"
```

### Task 8: Complete Desktop/TUI acceptance without capability leakage

- [x] **Step 1: Run Desktop unit, type, lint, and hard-gate E2E tests**

```bash
cd apps/desktop
npm run typecheck
npm run lint
npm test
npm run test:e2e -- e2e/auth-hard-gate.spec.ts
```

- [x] **Step 2: Run TUI and Python integration suites**

```bash
cd ../..
HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/tui_gateway tests/hermes_cli/client_auth tests/gateway/test_api_server.py -q
cd ui-tui
npm run check
```

- [x] **Step 3: Inspect secret-leak artifacts produced by tests**

Run the test harness with sentinel username, password, Session, and CSRF values, then scan only generated test logs, crash reports, captured argv/env, and temporary connection registries. The sentinel password and Cookie values must have zero matches. This is an artifact test, not a production-source regex assertion.

- [x] **Step 4: Record and commit acceptance evidence**

Create the initial Desktop/TUI/SSH sections of `docs/security/remote-auth-release-evidence.md` with the tested commit, exact commands, pass/fail totals, native OS/SSH fixture used, and locations of redacted test artifacts. Plan 4 extends this same file for server, background, container, and native-matrix acceptance rather than creating a second evidence authority. Record only test account labels and reason codes; do not include credentials, Cookies, bearer tokens, host private keys, or raw environment dumps.

```bash
cd ../
git diff --check
git add docs/security/remote-auth-release-evidence.md
git commit -m "test: verify desktop tui and ssh auth gate"
```
