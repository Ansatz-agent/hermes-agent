# Main Authentication and Voice Platform Boundary Design

## Goal

Make `main` the single source-runnable product baseline for Hermes authentication and Voice/SenseVoice. The macOS and Windows release branches must consume that common behavior and add only delivery concerns: installer construction, bundled payload materialization, packaged-runtime discovery, platform bootstrap, signing, artifact workflows, and installed-artifact evidence.

The primary boundary is capability ownership:

- Login policy, session lifecycle, hard-gate decisions, logout behavior, protected-entrypoint coverage, and Voice behavior are common product capabilities and belong to `main`.
- Keychain versus Windows Credential Manager and Unix socket versus Windows named pipe are internal operating-system adapters for those common capabilities. They also belong to `main` because source execution on each supported operating system must work without a release overlay.
- DMG, NSIS, packaged payloads, mirrors, signing, installer repair, quarantine handling, and exact-artifact test drivers are platform delivery capabilities and do not belong to `main`.

No macOS-only or Windows-only user-facing product capability is introduced by this migration.

## Locked references

The references for this reconstruction are:

| Role | Reference |
| --- | --- |
| Target base | `ansatz/main@9bd88c530716279a089ed18428dc785732b6e1be` |
| Accepted macOS product behavior and shipping source | `release/desktop-dmg-auth-e2e@80db6d8265f805cec46817d913982e4c5f6405c4` |
| Replayable macOS integration history | `integration/desktop-dmg-auth-e2e@403e1c3873d1679720c1403d7e38acd289804d69` |
| Accepted Windows product behavior | `integration/desktop-windows-auth-e2e@c2d3d09aab921130171ff611e260c13e9c6d477c` |
| Latest local Windows documentation tip | `integration/desktop-windows-auth-e2e@56b402c63b22da81f906ff1f7398a90cfd17bd81` |
| Original common authentication feature | `feature/remote-auth-hard-gate@763465daf019c8755813659b98a72c6c6f4662e3` |
| Original complete Voice feature | `feature/desktop-dmg-voice-confirmation@3ad4a126606079c77e7adca6d8661cd0c8c0a93b` |
| Shared pre-integration baseline | `4ef56cef4c6eecc009e2284fe2f1df20664f357a` |

`git ls-remote ansatz refs/heads/main` returned `9bd88c530716279a089ed18428dc785732b6e1be` on 2026-08-22. Implementation must repeat that check before the first source change and must stop if the remote tip moved.

### Why the replay source is not the release branch

`release/desktop-dmg-auth-e2e` and `integration/desktop-dmg-auth-e2e` are not in an ancestor relationship. Their merge base is the old shared baseline:

```bash
git merge-base release/desktop-dmg-auth-e2e integration/desktop-dmg-auth-e2e
# 4ef56cef4c6eecc009e2284fe2f1df20664f357a
```

The release branch starts from squash commit `553adec5b2` and contains the accepted shipping source. The integration branch preserves the individual commits needed for an auditable replay. Their final product code is equivalent; the 15 path differences are CI credential/Gatekeeper drivers and release documentation:

```bash
git diff --name-status 403e1c3873d1679720c1403d7e38acd289804d69 \
  80db6d8265f805cec46817d913982e4c5f6405c4
```

The implementation plan may therefore replay product commits from `403e1c3873`, but functional parity must always be measured against shipping reference `80db6d8265`.

### Path-count definition

Using the final release DMG reference `80db6d8265`, Windows behavior reference `c2d3d09aab`, and shared baseline `4ef56cef4c`, the two final branches have 285 common changed paths. Of those, 227 have identical final blobs and 58 differ.

The count is specifically the intersection of:

```bash
git diff --name-only 4ef56cef4c..80db6d8265
git diff --name-only 4ef56cef4c..c2d3d09aab
```

It must not be recomputed with `403e1c3873` substituted for the release reference, because the integration tree retains CI/evidence paths intentionally absent from the shipping release. Path intersection is only a discovery aid; responsibility and behavioral parity decide ownership.

## Required common behavior

### Authentication

`main` must provide the following behavior from source on macOS and Windows:

- The account server is fixed to `https://c2sml.cn/agent`.
- The client provides no registration, invitation, password-recovery, or account-creation surface.
- Session material is stored only through the operating-system secure credential abstraction. It is never written to config files, logs, install directories, bootstrap state, or renderer-readable diagnostics.
- Restored sessions are checked online before protected state is entered.
- Login, logout, expiry, revocation, Retry, bridge recovery, and owner recovery fail closed.
- Desktop GUI, CLI, Ink TUI, gateway, serve, cron, MCP, ACP, and background/service entrypoints all use the central Auth Guard.
- While signed out, only `hermes login`, `hermes logout`, `hermes auth status`, `hermes --help`, and `hermes --version` are allowed.
- Background entrypoints never prompt for a password and instruct the operator to run `hermes login`.
- Protected backend, Agent, gateway, HTTP/WS listener, protected renderer root, and protected IPC do not start or mount before authorization.
- Logout clears the current scope, stops acceptance of new tasks, suppresses results from the old runtime epoch, and immediately returns Desktop to the login gate.
- A fast logout followed by login to another account cannot publish the prior account's authenticated runtime state.
- Source execution and packaged execution use the same authorization decisions.

### Safe pre-authentication progress

The common layer owns the safe progress contract and login-gate presentation:

- `hermes:bootstrap:get` remains protected and returns `AUTH_REQUIRED` while signed out.
- The only signed-out bootstrap status channel is `hermes:auth-bootstrap:get`.
- Its payload is bounded, structured, sanitized, and contains no raw command, path, Cookie, Session, CSRF value, password, Keychain value, or terminal transcript.
- Unknown labels are sanitized and length-limited.
- A percentage is shown only when a producer supplies a real total.
- Retry is enabled only after a declared failure and never resubmits credentials.
- `runtime_ready` is part of the common account/runtime status. Protected product mounting requires both an authenticated scope and `runtime_ready`.
- `desktop-runtime-gate.ts`, `authenticated-runtime-preparation.ts`, `bootstrap-progress.ts`, the safe renderer progress components, and their state contracts are common product logic. A release overlay may produce progress events, but it may not redefine their safety or mounting semantics.

### Voice/SenseVoice

`main` owns the complete Voice behavior already accepted in the final macOS DMG:

- Desktop recording and composer controls.
- Automatic SenseVoice transcription.
- Readiness, download, retry, lazy dependency loading, and model-cache behavior.
- Voice timing, recorder teardown, and barge-in behavior.
- Provider configuration without inventing a provider requirement for local SenseVoice.
- Python transcription registry and tools.
- Shared configuration defaults, example configuration, types, and localization.
- Authentication protection for transcription endpoints and Voice UI placement behind the protected root.

A blob comparison confirmed that the Voice product paths in `3ad4a126606079c77e7adca6d8661cd0c8c0a93b` match the shipping DMG reference. Later commits touching those paths are authentication integration changes, not missing Voice features. The Voice commit is therefore the authoritative common Voice source.

Voice failure is isolated from authentication: a missing model or provider disables the affected Voice action but cannot block login, create an alternate protected entrypoint, or weaken the Auth Guard.

## Ownership boundary

### `main`: common capability and source-runtime adapters

`main` owns:

- `hermes_cli/client_auth/**`, common CLI commands, entrypoint wrappers, manifest, static help, and native evidence tools.
- The central guard wiring across Desktop, CLI, TUI, gateway, serve, cron, MCP, ACP, Docker/s6 background services, and direct Python entrypoints.
- Electron auth bridge/coordinator/scope, guarded IPC, safe preload APIs, backend ownership, protected-root ordering, safe progress contract, runtime epoch suppression, and GUI logout.
- Cross-platform secure-credential and local-owner transports required for source mode, including macOS secure-store/Unix transport behavior and Windows Credential Manager/named-pipe behavior behind the same interfaces.
- Platform-neutral Electron hardening that protects common authenticated behavior, such as trusted-renderer, external-open, media-permission, preview-webview, and normalized renderer logging policies when they are not coupled to an installer.
- The complete Voice/SenseVoice source implementation and source-install dependency declarations.
- Neutral `desktop-bundle` update messaging used by shared product files. Platform overlays must not fork `hermes_cli/config.py`, `update_cmd.py`, or `web_server.py` merely to say “DMG” or “installer”.
- Common source-mode tests, the multi-OS native authentication evidence matrix, functional-parity inventories, and platform-boundary checks.

OS-specific adapter code is permitted in `main` only when all of these conditions hold:

1. It implements a common authentication or Voice interface.
2. Source execution on that OS requires it.
3. It does not inspect `process.resourcesPath`, packaged payloads, installer state, or release artifacts.
4. Its behavior is covered by source-mode tests on the corresponding OS.

### macOS release overlay

The macOS branch owns only:

- DMG/Electron Builder mac artifact configuration.
- Bundled backend and authentication-toolchain payload generation, hashing, staging, signing, and validation.
- Packaged-resource lookup and macOS runtime placement.
- Shell bootstrap used only by the installed application, domestic mirrors used only for packaged first launch, install markers, and platform repair.
- Code signing, notarization, Gatekeeper/quarantine, architecture, mounted-DMG, installed-App, and exact-artifact tests.
- macOS release workflows, CI credential drivers, build documentation, and sanitized acceptance evidence.

It must not modify common authentication policy, `client_auth/**`, guarded IPC policy, Auth Gate behavior, GUI logout, runtime epoch handling, or Voice behavior.

### Windows release overlay

The Windows branch owns only:

- NSIS/Electron Builder Windows artifact configuration.
- Bundled runtime/payload staging, packaged Git/uv/Python discovery, and PowerShell installer/bootstrap used by the installed application.
- Windows installer markers, packaged process containment, installer recovery, and installed-artifact runtime discovery.
- Windows packaging workflows, exact-artifact drivers, clean-VM installed tests, build documentation, and sanitized release evidence.

It must not delete the common GUI logout item, fork common update messaging, keep a private copy of `client_auth/runtime.py`, or redefine Auth Guard/Voice behavior.

### Explicitly forbidden in `main`

The common branch must reject:

- `desktop_auth_runtime/**` and minimal-auth bundled lock projects.
- DMG/NSIS contracts and artifact builders.
- `bootstrap-payload*`, `bootstrap-toolchain*`, `bundled-runtime*`, and packaged backend/auth-toolchain materialization.
- Packaged-resource lookup using `process.resourcesPath`.
- Signing, notarization, Gatekeeper/quarantine, and platform-mirror configuration.
- `.github/workflows/desktop-windows-package.yml`, `.github/workflows/desktop-dmg-gatekeeper.yml`, and exact-artifact credential drivers.
- Packaged-only PowerShell and shell tests, while retaining the existing generic source-installer tests already present in `main`.
- DMG/Windows release evidence and platform packaging design documents.

## Dependency ownership

`main` owns dependencies required to run common authentication and Voice from source, including `keyring` and the `sensevoice` extra. Release-only dependency pinning, bundled offline projects, and packaged-browser/toolchain locks remain overlays.

Before implementation, the plan must resolve `pyproject.toml`, `uv.lock`, and `uv.toml` explicitly:

- The common lock is regenerated from the common dependency declarations; it is not copied from a packaged runtime.
- `uv lock --check` is used only after regeneration.
- Any `exclude-newer` timestamp retained in `main` must be justified as repository-wide source reproducibility, not as a bundled Desktop workaround.
- `setuptools==83.0.0` remains in `main` only if source authentication or Voice requires it; a bundled-runtime-only pin stays in an overlay.

## Migration strategy

Use a clean-base reconstruction; never merge either shipping platform branch wholesale:

1. Reconfirm `ansatz/main` and the locked reference SHAs.
2. Add an executable allowlist-based common-branch boundary checker and remove the misplaced Windows packaging workflow already present in the target base.
3. Merge the reviewed original authentication history.
4. Replay every accepted common authentication commit in original dependency order, including safe bootstrap IPC/progress, GUI logout, bridge recovery, `runtime_ready`, runtime epoch suppression, and native owner recovery.
5. Cherry-pick the complete Voice commit, resolving overlapping files by preserving both authentication and Voice.
6. Extract the source-runtime portions of the accepted Windows owner/deadline/named-pipe work into the common implementation; do not import Windows packaging files.
7. Reconcile dependencies and regenerate deterministic inventories without losing any protected entrypoint.
8. Prove product-file parity against the shipping DMG reference, with written waivers for intentional neutralization or Windows source support.
9. Run macOS and Windows source-mode acceptance before proposing a merge into `main`.
10. Only after approval, merge the common branch and recreate the platform release branches as thin overlays based on the new `main`.

The implementation plan contains the complete commit classification and is authoritative for replay. Handwritten reconstruction is not an acceptable substitute when an accepted product commit exists.

## Required proof gates

Passing tests alone does not prove equivalence. The candidate must pass all of the following:

1. **Product-file parity:** a versioned manifest lists every common product path expected from the final DMG. `git diff <candidate> 80db6d8265 -- <manifest paths>` must be empty except for reviewed waivers describing neutral platform wording, Windows source adapters, or newer common tests.
2. **Reverse boundary:** every path changed from `ansatz/main` must be covered by an approved `main` ownership rule. Packaging-like new files fail by pattern even when their exact names were not known when the checker was written.
3. **Entrypoint non-regression:** the generated common entrypoint manifest contains every non-installer protected entrypoint present in the accepted DMG manifest.
4. **Signed-out IPC isolation:** `hermes:bootstrap:get` returns `AUTH_REQUIRED`; only sanitized `hermes:auth-bootstrap:get` is available to the login surface.
5. **Account epoch isolation:** logout and rapid account switching cannot publish a prior epoch's authenticated runtime status.
6. **macOS source acceptance:** login, logout, restore plus online validation, expiry/revocation relock, all-entrypoint rejection, backend lifecycle, and Voice smoke.
7. **Windows source acceptance:** the same behavior using the Windows secure-store and named-pipe owner transport, including cold-start and Retry races.
8. **Voice parity:** the accepted Voice paths remain blob-equivalent to `3ad4a12660` unless a documented authentication-integration change is required.

## Runtime flows

### Source execution

1. Only the public authentication surface or an allowed public command starts.
2. The common runtime reads the OS secure store through its adapter.
3. A restored session is validated online.
4. The common auth scope is issued for the current session epoch.
5. Guarded IPC and protected backend startup become available.
6. The protected root mounts exactly once after both auth scope and common runtime readiness are true.
7. Logout or remote invalidation revokes the scope, increments the epoch, ignores stale preparation results, stops new work, and returns to the gate.

No installer or bundled payload participates in source execution.

### Packaged execution

1. The platform overlay prepares only the minimal login runtime without starting protected Hermes services.
2. The common login/session flow runs unchanged.
3. After authentication, the overlay prepares the full packaged runtime and emits only sanitized common progress events.
4. The common runtime gate sets `runtime_ready` after validated preparation.
5. The common Auth Guard mounts the protected product.

## Failure and security invariants

- Every missing dependency, credential-store error, bridge failure, timeout, network error, malformed progress event, or owner race remains locked.
- Retry repeats only the failed safe operation and never reuses a password.
- Raw installer output never reaches the renderer.
- Protected IPC stays fail-closed during bootstrap and teardown.
- Voice failures never become authentication failures and never open an alternate service path.
- Platform overlays cannot override auth scope or runtime epoch decisions.
- Logs and evidence remain sanitized and never include credentials or session material.

## Intended branch graph

```text
ansatz/main
└── common authentication + common Voice/SenseVoice + required source-runtime OS adapters
    ├── release/desktop-dmg-auth-voice-v2
    │   └── macOS delivery overlay only
    └── release/desktop-windows-auth-voice-v2
        └── Windows delivery overlay only
```

The current DMG and Windows branches remain immutable behavior/artifact references until both replacement overlays pass installed-artifact acceptance. This design does not authorize a merge into `main`.
