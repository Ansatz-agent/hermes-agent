# Main Auth and Voice Platform Boundary Design

## Goal

Make `main` the runnable, cross-platform product baseline for Hermes authentication and voice. macOS DMG and Windows release branches must add only the installer, packaging payload, platform bootstrap, and artifact-specific verification required by that distribution.

## Baselines and authoritative implementations

- Target base: `ansatz/main@9bd88c530716279a089ed18428dc785732b6e1be`.
- Validated macOS behavior: `release/desktop-dmg-auth-e2e@80db6d8265f805cec46817d913982e4c5f6405c4`.
- Validated Windows behavior: `integration/desktop-windows-auth-e2e@c2d3d09aab921130171ff611e260c13e9c6d477c`.
- Original authentication feature: `feature/remote-auth-hard-gate@763465daf019c8755813659b98a72c6c6f4662e3`.
- Original voice feature: `feature/desktop-dmg-voice-confirmation@3ad4a126606079c77e7adca6d8661cd0c8c0a93b`.

The migration must preserve authorship where commits can be replayed without importing platform packaging. The validated macOS and Windows branches are behavioral references, not branches to merge wholesale.

## Required behavior in `main`

`main` must be directly runnable from source on macOS and Windows. Source execution is not an authentication bypass.

Authentication behavior in `main` includes:

- The fixed account server `https://c2sml.cn/agent`.
- Online authentication before any protected Hermes runtime or interface starts.
- Central Auth Guard enforcement for Desktop GUI, CLI, Ink TUI, gateway, serve, cron, MCP, ACP, and background/service entrypoints.
- Only `hermes login`, `hermes logout`, `hermes auth status`, `hermes --help`, and `hermes --version` are usable while signed out.
- Background entrypoints never request a password and direct the user to `hermes login`.
- Session material is stored only through the operating system secure credential store. It is never written to config files, logs, install directories, or bootstrap state.
- The client offers no registration, invitation, password recovery, or account creation surface.
- Login, logout, online session revalidation, expiry/revocation relocking, protected IPC rejection, and backend shutdown behavior are shared product behavior.
- Authentication errors fail closed and expose only normalized, non-sensitive reasons.

Voice behavior in `main` includes:

- Desktop recording and voice composer behavior.
- SenseVoice transcription integration and readiness handling.
- Voice timing, barge-in, automatic dictation, provider configuration, and shared localization.
- Python transcription registry and transcription tools.
- Voice unit and integration tests that do not depend on a packaged installer.

Voice failure must remain isolated from authentication: unavailable voice models or providers may disable voice actions, but must not prevent login or weaken the Auth Guard.

## Code ownership boundary

### `main` owns product runtime behavior

`main` owns code that answers what authentication or voice does, regardless of installation method:

- Authentication client, session runtime, central guard, entrypoint wrappers, and safe status protocol.
- Desktop login/logout UI, protected root, guarded IPC, and Electron-to-Python authentication protocol.
- Secure credential storage calls through the cross-platform credential abstraction.
- Platform-neutral authentication state, runtime scope, progress event contracts, and renderer state.
- Voice and SenseVoice application/runtime code.
- Root dependency declarations required to run authentication and voice from source.
- Cross-platform behavior tests and static entrypoint inventories.

Code may branch on `process.platform` or the Python platform only when the operating system capability is part of runtime behavior, such as selecting a secure credential store. It must not resolve packaged resources or installer locations in `main`.

### macOS DMG release branch owns delivery to macOS

The macOS release overlay owns:

- DMG build commands and Electron Builder macOS artifact configuration.
- Bundled backend and authentication payload generation, signing, hashing, staging, and validation.
- macOS shell installation/bootstrap, mirror selection, runtime placement, and install markers.
- Gatekeeper, quarantine, architecture, signature, mounted-DMG, installed-App, and exact-artifact tests.
- macOS packaging documentation and release evidence.

It must not redefine authentication policy or voice behavior.

### Windows release branch owns delivery to Windows

The Windows release overlay owns:

- NSIS and Windows Electron Builder artifact configuration.
- PowerShell installation/bootstrap, payload staging, managed runtime placement, and install markers.
- Windows packaged-process containment, installer recovery, and artifact-specific runtime discovery.
- Windows packaging workflow and clean-VM installed-artifact tests.
- Windows packaging documentation and release evidence.

It must not redefine authentication policy or voice behavior.

### Packaging metadata is not product runtime

Minimal-auth lock projects, payload manifests, bundled-resource lookup, `process.resourcesPath` handling, platform installer scripts, build workflows, and exact-artifact drivers stay out of `main` even when macOS and Windows currently contain similar copies. A shared runtime protocol can live in `main`; the files that materialize that protocol into an installer remain release overlays.

The current `.github/workflows/desktop-windows-package.yml` at `ansatz/main@9bd88c5` is therefore removed from the common product branch and retained in the Windows packaging branch.

## Migration strategy

Use a clean-base reconstruction rather than merging either final packaging branch wholesale:

1. Start `integration/main-auth-voice-base` from `ansatz/main@9bd88c5`.
2. Remove the Windows packaging workflow from the common branch.
3. Replay the original authentication and voice feature commits while preserving authorship.
4. Replay cross-platform fixes that are required by both validated release branches.
5. For files where macOS and Windows differ, extract or retain a platform-neutral runtime core and move resource discovery, installer execution, and packaged-runtime validation into the respective release overlays.
6. Verify the common tree against both validated branches so no authentication or voice regression is lost.
7. Merge the reviewed common branch into `main` without importing either platform installer.
8. Create new macOS and Windows release branches from the updated `main`, then apply only their platform overlays. Preserve the existing release branches as historical acceptance references; do not force-rewrite them.

The current final branches change 285 common paths relative to the old baseline. Of those, 227 have identical final blobs and 58 differ. Identical files are strong common-layer candidates, but inclusion is decided by responsibility, not by automatic file intersection.

## Runtime flows

### Source execution

1. A permitted public command or the authentication UI starts.
2. The common authentication runtime checks the operating system secure credential store.
3. Any restored session is validated online against the fixed server.
4. Only an authenticated scope can open protected IPC or start a protected backend/entrypoint.
5. Logout or remote session invalidation revokes the local scope, stops accepting new work, and returns Desktop to the login gate.

No installer or bundled payload is involved in this flow.

### Packaged execution

1. The platform release overlay prepares the minimal authentication runtime without starting Hermes backend services.
2. The common authentication UI and protocol perform the same source-runtime login flow.
3. After authentication succeeds, the platform overlay prepares the full Hermes runtime and emits sanitized common progress events.
4. The common Auth Guard mounts the protected product only after both authenticated scope and runtime readiness are true.

## Failure handling and security invariants

- Missing dependencies, credential-store failures, bridge failures, timeouts, and network errors remain locked states.
- Retry restarts only the failed safe operation and never reuses or retransmits a password automatically.
- Installer output is sanitized before it becomes a progress event. Raw commands, paths, cookies, sessions, CSRF values, passwords, or credential-store contents never reach the renderer.
- Unknown or malformed progress events are ignored or converted to a safe failure; percentages are shown only when the producer supplies a real total.
- Voice initialization and model download failures surface as voice-specific errors and never open protected product surfaces.
- Packaging overlays may implement recovery for their own runtime, but cannot override Auth Guard decisions.

## Verification gates

### Common `main` gate

- Python authentication suite, entrypoint inventory, static help generator, and credential-storage tests.
- Desktop typecheck, lint, relevant Vitest, and Auth Guard E2E in source mode.
- Signed-out rejection tests for GUI, CLI, TUI, gateway, serve, cron/background, MCP, and ACP.
- Login, restored-session online validation, logout, expiry/revocation, protected IPC, and backend lifecycle tests.
- Voice/SenseVoice Python and Desktop tests, including provider readiness and failure isolation.
- Ruff, YAML, shell syntax for common scripts, lock consistency, and `git diff --check`.
- A boundary test that rejects DMG, NSIS, platform installer, bundled payload, and packaging-workflow files in the common branch.

### macOS release overlay gate

- Common gate plus DMG contracts, payload integrity, real DMG build, Gatekeeper/quarantine, clean install, login/logout, Voice smoke, and installed-App execution.
- An artifact inventory proving that Windows workflows, NSIS, PowerShell installers, and CI credential drivers are absent from the DMG.

### Windows release overlay gate

- Common gate plus NSIS contracts, payload integrity, real installer build, clean-VM install, login/logout, Voice smoke, and installed-App execution.
- An artifact inventory proving that DMG scripts, macOS shell bootstrap, signing assets, and macOS release evidence are absent from the installer.

Temporary CI-only verification files may exist on a separate verification branch, but must not be merged into `main` or either shipping source branch.

## Branch result

The intended branch graph is:

```text
ansatz/main
└── authentication + Voice/SenseVoice + cross-platform runtime behavior
    ├── release/desktop-dmg-auth-voice-v2
    │   └── macOS installer and DMG packaging overlay only
    └── release/desktop-windows-auth-voice-v2
        └── Windows installer and packaging overlay only
```

The existing DMG and Windows integration branches remain available as immutable behavioral and artifact references until both replacement release branches pass full acceptance.
