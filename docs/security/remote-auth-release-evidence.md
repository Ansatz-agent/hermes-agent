# Remote authentication release evidence

This file is the release-evidence authority for the Hermes client hard gate. It records only test account labels, public reason codes, and redacted artifact metadata. Credentials, Cookies, bearer tokens, private host keys, and raw environment dumps must never be added here.

## Desktop, TUI, and SSH acceptance — 2026-08-19

- Tested implementation commit: `ef0ee016c98cef69b766ddc8d3e0f0fb1b1011f8`
- Branch: `feature/remote-auth-hard-gate`
- Host: Darwin 25.3.0 arm64
- Runtime: Node v25.1.0, npm 11.6.2, Python 3.11.5
- SSH client: OpenSSH_9.5p1 with OpenSSL 3.6.2
- Native SSH fixture: command-construction doubles for `ssh` and bounded non-shell `ssh-keyscan`, temporary owner-only `known_hosts` files, and real loopback listeners for tunnel readiness. No production host, production key, or production credential was used.

### Results

| Surface | Command | Result |
| --- | --- | --- |
| Desktop types | `cd apps/desktop && npm run typecheck` | Pass |
| Desktop lint | `cd apps/desktop && npm run lint` | Pass, 0 errors; 107 existing warnings |
| Desktop unit/integration | `cd apps/desktop && npm test` | 566 files passed, 1 skipped; 5,574 tests passed, 2 skipped |
| Desktop hard-gate E2E | `cd apps/desktop && npx playwright test e2e/auth-hard-gate.spec.ts` | 1 passed |
| Ink TUI build/type/test/lint | `cd ui-tui && npm run check` | 155 files passed; 1,636 tests passed, 1 skipped; lint 0 errors and 2 existing warnings |
| Python auth, CLI/TUI, gateway | `HERMES_PYTHON=../../.venv/bin/python scripts/run_tests.sh tests/tui_gateway tests/hermes_cli/client_auth tests/gateway/test_api_server.py -q` | 72 files; 702 passed, 7 platform skips, 0 failed |
| Desktop/SSH artifact harness | redacted sentinel environment + five Electron auth/SSH suites with Vitest JSON output | 5 suites; 93 passed, 0 failed |
| Python artifact harness | redacted sentinel environment + client, bridge, runtime, and boundary suites with JUnit output | 94 passed, 7 skipped, 0 failed |

The npm `test:e2e` script hard-codes the complete `e2e/` directory, so appending a file path does not select one file. The intended hard-gate acceptance was therefore run directly with Playwright as shown above. The E2E verifies that the unauthenticated renderer does not load the protected root, six protected Desktop IPC capability classes return `AUTH_REQUIRED`, auxiliary windows cannot be opened, and no Hermes backend process starts.

The Python acceptance found and closed a long-process gap: an already connected TUI WebSocket now calls `require_authorized("tui.ws.request")` immediately before every RPC and closes with code 4401 after owner EOF, logout, expiry, or server rejection. The full Python suite was rerun after this fix.

### Secret-leak artifact inspection

The harness ran with separate non-production username, password, Session, and CSRF sentinels. Password, Session, and CSRF values were redacted from the command record below and had zero matches when scanning only the generated artifacts. No crash report was generated.

- Artifact directory: `/private/tmp/hermes-remote-auth-acceptance.UUAe9t`
- Desktop JSON: `desktop-vitest.json`, 26,250 bytes, SHA-256 `c95dc90bbfef55cce7ad12ecc89d642b3d016e936f7665ecb2273b59881c60c6`
- Python JUnit: `python-junit.xml`, 17,628 bytes, SHA-256 `64e6144fdb0f8a69c477fd03b05cb2303dfd15cb2b831c6ab5b77ce3559dc812`
- Scan scope: the two files above only
- Scan result: password 0 matches; Session 0 matches; CSRF 0 matches

The raw artifact directory is intentionally ephemeral and is not committed. The targeted tests capture and assert the redacted Desktop bridge diagnostics, SSH/backend argv and environment, guarded IPC errors, Python owner argv and environment, and process-local connection-scope registry behavior before the result reporters write these artifacts.

### Platform coverage boundary

The native run covers macOS arm64. Seven Python cases were skipped because they are explicitly Linux- or Windows-only; their required lanes are the main Linux CI lane and `tests-os` on `windows-latest`. Server, background-service, headless-container, and full native-matrix evidence will extend this same file in the next release plan.

## Background, container, and audit remediation — 2026-08-19

- Implementation commits: `a6dcb95`, `0769289`, `cee8bdb`
- Branch: `feature/remote-auth-hard-gate`
- Local host: Darwin 25.3.0 arm64

### Closed review findings

- Logout now rotates `runtime_instance_id`, so a consumer or scope captured before logout cannot revive after a later login in the same process.
- The installed `hermes_cli.main:main` callable catches `AuthRequired`, emits one redacted `AUTH_REQUIRED` line, and exits `20` without a traceback.
- CI directly checks the generated entrypoint manifest and static auth-free help artifact. Manifest tests now explicitly account for `guarded`, `auth-shell`, and `locked-waiting` startup classes.
- Desktop IPC moved zoom, title-bar/native theme, and translucency out of `auth-free`; the remaining unauthenticated IPC allowlist is limited to account/bootstrap/startup-progress and redacted renderer-error functions.
- systemd, launchd, Windows Scheduled Tasks, the deprecated kanban unit, Docker CMD, dashboard, and dynamic profile gateways enter one shared noninteractive `locked-waiting` runtime before capability startup. They never prompt for a password.
- Docker reconciliation preserves desired gateway intent but registers every static and profile capability down. The s6 auth owner runs as `hermes` with an ephemeral `0700` runtime outside `HERMES_HOME`; login/auth transitions are the only path that applies desired up/down state.
- Native Linux/macOS/Windows jobs now publish a strict transport/locked-start/handle-noninheritable/service-waiting JSON artifact. A separate job rejects missing, false, duplicate-disagreeing, or wrong-transport evidence.
- The Docker CI test is a real s6 lifecycle test, not an image-presence assertion: it checks signed-out boot, non-root owner identity, runtime permissions, capability-down state, forced-up entry backstop, absence of a listening dashboard port, redacted storage, and signed-out reboot.

The Desktop connection ID remains an execution-target selector, not an authorization token. Main-process registry lookup and the per-target Auth Coordinator must both succeed for that exact connection before a handler runs; selecting a different authenticated target does not bypass login, while binding the main workspace renderer to one connection would break the supported multi-connection UI. This was therefore retained as an intentional routing design, with exact-scope authorization as the security boundary.

### Local verification

| Surface | Result |
| --- | --- |
| Python remote-auth suite | 149 passed, 7 platform skips |
| Background/manifest/native-artifact focused tests | 21 passed |
| Host service generation | 91 passed, 3 platform skips |
| Container host-side tests | 7 passed; 6 real-container tests skipped because no local Docker daemon was available |
| Gateway/cron/MCP/tool boundary regression | 330 passed; one pre-existing Linux abstract-socket test deselected on macOS |
| Desktop typecheck and targeted auth/IPC/startup tests | Typecheck passed; 26 tests passed |
| Entry/help generators, YAML parsing, shell syntax, Ruff, diff whitespace | Passed |

The real Docker lifecycle and the complete three-OS native artifact matrix are required by CI and are not represented as local passes in this record. No production credential or raw Session material was used in these remediation tests.
