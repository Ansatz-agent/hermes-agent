# Main Authentication, Voice, and Cross-Platform Packaging Foundation Design

## Goal

Make `main` the authoritative implementation of Hermes authentication, Voice/SenseVoice, and the build-time foundation required to package those capabilities for macOS and Windows.

The completed `main` candidate must satisfy all three hard requirements:

1. Authentication and Voice behavior matches the accepted macOS DMG source at `release/desktop-dmg-auth-e2e@80db6d8265f805cec46817d913982e4c5f6405c4`, except for reviewed operating-system adapters.
2. Authentication dependencies, the post-login Hermes runtime, and every Hermes-managed transitive, optional, repair, or lazy dependency use a controlled domestic-mirror-first download policy, with integrity validation and bounded fallback.
3. Running `dist:mac:dmg` on macOS or `dist:win:nsis` on Windows produces an installer that can be installed on a clean computer, authenticate, prepare the runtime, use Voice/SenseVoice, and log out correctly.

This is stronger than source compatibility or a successful Electron compile. A package that opens but cannot authenticate on a clean machine does not meet the goal.

## Current `main` packaging baseline

The locked target base, `ansatz/main@9bd88c530716279a089ed18428dc785732b6e1be`, already contains a cross-platform Electron Builder skeleton:

- macOS targets: DMG and ZIP, entitlements, Hardened Runtime, icons, and notarization hooks.
- Windows targets: NSIS and MSI, selectable installation directory, shortcuts, uninstall metadata, and executable identity handling.
- `before-pack.mjs` stages `node-pty` and `get-windows` native files for the actual target platform and architecture.
- `after-pack.mjs` applies the Windows executable identity.
- `install.sh` and `install.ps1` already contain a last-resort npmmirror fallback for Electron.

The base is not yet a clean-machine production package for the required authentication flow. Electron Builder currently includes the compiled Desktop shell and selected resources, but the complete Python/Hermes backend is not bundled. The first-run installer fetches dependencies after installation. The final DMG branch added the missing authentication bootstrap, bundled toolchain, runtime preparation, progress reporting, mirror policy, and repair behavior.

The migration therefore preserves the existing dual-platform skeleton and completes it. It does not remove or freeze the skeleton merely because its files are platform-aware.

## Locked references

| Role | Reference |
| --- | --- |
| Target base | `ansatz/main@9bd88c530716279a089ed18428dc785732b6e1be` |
| Accepted macOS behavior and shipping source | `release/desktop-dmg-auth-e2e@80db6d8265f805cec46817d913982e4c5f6405c4` |
| Replayable macOS integration history | `integration/desktop-dmg-auth-e2e@403e1c3873d1679720c1403d7e38acd289804d69` |
| Accepted Windows behavior | `integration/desktop-windows-auth-e2e@c2d3d09aab921130171ff611e260c13e9c6d477c` |
| Latest Windows documentation tip | `integration/desktop-windows-auth-e2e@56b402c63b22da81f906ff1f7398a90cfd17bd81` |
| Original common authentication feature | `feature/remote-auth-hard-gate@763465daf019c8755813659b98a72c6c6f4662e3` |
| Original complete Voice feature | `feature/desktop-dmg-voice-confirmation@3ad4a126606079c77e7adca6d8661cd0c8c0a93b` |
| Shared pre-integration baseline | `4ef56cef4c6eecc009e2284fe2f1df20664f357a` |

`release/desktop-dmg-auth-e2e` is the behavioral and shipping reference. `integration/desktop-dmg-auth-e2e` supplies the auditable commit sequence because the release branch was reconstructed as a squash and is not its descendant. The known 15-path difference consists of CI credential/Gatekeeper drivers and release evidence, not accepted product behavior.

Implementation must recheck every locked reference and the remote `ansatz/main` tip before replaying source changes. A moved base requires a documented rebase and renewed review.

## Required authentication behavior

The following behavior is identical in source mode and in packages produced for macOS or Windows:

- The account server is fixed to `https://c2sml.cn/agent`.
- The client exposes no registration, invitation, account creation, or password recovery.
- Session material is stored only through the operating-system secure credential abstraction: macOS Keychain or Windows Credential Manager.
- Session material is never written to configuration, logs, installation directories, bootstrap snapshots, evidence, or renderer-readable diagnostics.
- A restored session is validated online before protected state is entered.
- Login, logout, expiry, revocation, Retry, auth-bridge recovery, and owner recovery fail closed.
- Desktop GUI, CLI, Ink TUI, gateway, serve, cron, MCP, ACP, Docker/s6, background services, and direct Python entrypoints use the central Auth Guard.
- While signed out, only `hermes login`, `hermes logout`, `hermes auth status`, `hermes --help`, and `hermes --version` are available.
- Background entrypoints never prompt for a password and instruct the operator to run `hermes login`.
- Protected backend, Agent, gateway, HTTP/WS listeners, renderer root, and protected IPC do not start or mount before authorization.
- Logout clears the current scope, refuses new work, invalidates the runtime epoch, suppresses stale results, stops protected runtime activity, and immediately returns the Desktop to the login gate.
- A rapid logout followed by login to a different account cannot expose the prior account's runtime state.

The final DMG is the parity oracle. A main implementation is not accepted merely because it is similar or passes newly written tests; accepted product paths and black-box flows must be compared against `80db6d8265`.

## Safe bootstrap and progress behavior

The login gate, runtime gate, progress schema, sanitization, snapshot recovery, timeout behavior, and Retry rules are common product behavior and live in `main`.

- Before authentication, only the minimum authentication runtime may be prepared. No protected Hermes service starts.
- `hermes:bootstrap:get` remains protected and returns `AUTH_REQUIRED` while signed out.
- The only signed-out status channel is `hermes:auth-bootstrap:get`.
- Progress events are bounded and structured. They may contain a stage identifier, sanitized label, units, completed value, total value, state, and elapsed time.
- Raw commands, terminal output, paths, credentials, Cookies, Session values, CSRF values, secure-store contents, and arbitrary environment values never reach the renderer.
- A percentage is displayed only when a producer supplies a real total.
- Long-running work continues to show progress and does not become a false 15-second service error.
- Retry becomes available only after an explicit failure and never resubmits a username or password.
- Window reload or App restart restores the latest sanitized progress snapshot.
- `runtime_ready` and the authenticated runtime epoch jointly gate the protected renderer and backend.

## Voice/SenseVoice behavior

`main` owns the complete accepted Voice behavior:

- Desktop recording and composer controls.
- Automatic SenseVoice transcription.
- Readiness, model download, retry, lazy dependency loading, and model-cache behavior.
- Voice timing, recorder teardown, and barge-in behavior.
- Local SenseVoice without an invented remote provider requirement.
- Python transcription registry and tools.
- Shared configuration defaults, example configuration, types, and localization.
- Authentication protection for transcription endpoints and Voice UI placement behind the protected root.

Voice failure is isolated from authentication. A missing model, provider, microphone permission, or transcription dependency disables only the affected Voice operation; it cannot block account login, weaken the Auth Guard, or create an alternate protected entrypoint.

## Domestic-mirror-first dependency policy

The mirror policy belongs to `main` because packages built directly from `main` must work on clean computers in mainland China. It applies to both authentication bootstrap dependencies and the post-login Hermes/Voice runtime.

The default ordered policy is:

| Dependency class | Primary | Secondary or bounded fallback |
| --- | --- | --- |
| Python packages | `https://mirrors.ustc.edu.cn/pypi/simple` | `https://pypi.tuna.tsinghua.edu.cn/simple`, then the official index only after both domestic attempts fail within their deadlines |
| npm packages | `https://registry.npmmirror.com` | official registry after a bounded failure |
| Node distributions | `https://registry.npmmirror.com/-/binary/node/` | pinned official distribution after a bounded failure |
| Playwright browsers | `https://registry.npmmirror.com/-/binary/playwright` | pinned official download after a bounded failure |
| Electron distributions | `https://npmmirror.com/mirrors/electron/` | existing official source/fallback ordering, bounded by timeout |

Mirror priority never weakens supply-chain validation:

- Versions are pinned by the common lock or a versioned payload manifest.
- Downloaded archives and prebuilt toolchains require expected SHA-256 values before use.
- Bootstrap subprocesses receive a sanitized environment. Untrusted inherited `PIP_*`, `UV_*`, npm, Node, Electron, and Playwright mirror variables cannot silently redirect protected installation.
- Mirror attempts have idle and total deadlines and produce sanitized stage errors.
- Partial or corrupt downloads are not marked installed and are recoverable on Retry.
- No mirror request contains account credentials, session material, or authentication headers for `c2sml.cn`.

The minimum authentication runtime is bundled into each installer, so a clean computer does not have to reach GitHub, PyPI, or npm before showing and submitting the login form. Domestic mirrors are used when producing that bundled runtime and for any validated repair. After login, the same domestic-first ordering applies while materializing the full Hermes/Voice runtime.

### Recursive coverage

Domestic-first is an end-to-end installation invariant, not an environment setting applied only to the first command. It covers every download initiated or delegated by Hermes during build, installation, first launch, repair, update, and later lazy feature preparation:

- all direct and transitive Python wheels and source distributions resolved by uv or pip;
- uv-managed tools, including dependencies installed by `uv tool install` or equivalent lazy installers;
- all direct, transitive, optional, and native npm packages plus downloads initiated by npm lifecycle scripts;
- Node, Electron, Playwright browsers, managed Python, uv, Git, ripgrep, ffmpeg, and other Hermes-managed toolchain archives;
- the SenseVoice runtime and model archive, retaining ModelScope as the first model source and a hash-pinned fallback chain;
- Browser Use, Computer Use, and other optional Hermes features when Hermes performs their installation;
- child installers and subprocesses launched by `install.sh`, `install.ps1`, Electron bootstrap, repair, update, or feature-enablement code.

Every Hermes-owned download entrypoint is registered in a versioned origin manifest with its domestic primary, domestic secondary when available, official fallback, timeout, expected integrity mechanism, and owning test. An unregistered network origin in an installation or lazy-dependency path fails the boundary check.

Child processes receive the sanitized mirror policy through the variables understood by that tool, not through an unrestricted inherited environment. A child script may not clear the policy, switch to an official source first, execute an unverified remote script, or resolve an unlocked dependency. If a third-party installer cannot honor the policy and integrity contract, Hermes must replace it with a pinned direct download, bundle the dependency, or fail with a clear manual-action message.

This policy applies to downloads managed by Hermes. It does not rewrite user-configured model providers, account-server traffic, arbitrary plugin network calls, or the user's global Homebrew, winget, npm, pip, or uv configuration.

## Packaging foundation owned by `main`

`main` owns everything required for the two supported build commands to produce clean-machine-capable installers:

- The existing Electron Builder macOS and Windows targets and shared build hooks.
- Target-aware native dependency staging.
- The common authentication, runtime, Voice, safe-progress, and repair state machines.
- The minimal pre-authentication runtime project and its reproducible payload builder.
- The post-login Hermes/Voice payload or reproducible runtime-materialization builder.
- Versioned payload manifests, hashes, architecture checks, and packaged-resource discovery.
- macOS and Windows adapters for secure credentials, owner transport, process containment, runtime placement, and installation repair.
- Domestic-mirror-first configuration and bounded official fallback.
- Build contracts that fail if a required clean-install resource was not generated or staged.
- Package-content tests proving required resources are present and disallowed secrets or CI material are absent.

Generated payload archives, downloaded runtimes, credentials, certificates, notarization tickets, logs, and installers are build artifacts. They are not committed to `main`.

Platform-specific code is allowed in `main` when it is required by `dist:mac:dmg` or `dist:win:nsis`, implements the same product contract, and has target-platform tests. Platform differences may affect paths, process APIs, credential stores, signing mechanisms, and installer formats; they may not fork authentication or Voice policy.

## Workflow and release-infrastructure boundary

GitHub Actions workflows are repository automation, not App payload. Electron Builder's explicit `files` and `extraResources` lists exclude `.github/**`, source tests, evidence, and workflow files from DMG, ZIP, NSIS, and MSI artifacts.

The existing Windows workflow may be repaired and a macOS packaging workflow may be added as independent CI infrastructure. Their presence in the repository does not mean they are packaged. Tests must inspect the final artifact to prevent accidental inclusion.

Workflow responsibilities are limited to:

- selecting a trusted target runner;
- installing the locked Node, npm, Python, and uv toolchains;
- invoking the same build commands used locally;
- supplying signing/notarization secrets without persisting them;
- running exact-artifact acceptance;
- publishing sanitized evidence and artifacts.

Workflows may not introduce alternate authentication behavior, CI-only product fallbacks, fake login success, or files required only to make CI pass. A locally built release and CI-built release from the same commit must have the same product behavior.

## Source and packaged flows

### Source execution

1. Only the public authentication surface or an allowed public command starts.
2. The common runtime reads the operating-system secure store.
3. A restored session is validated online.
4. The common auth scope is issued for the current epoch.
5. Guarded IPC and protected backend startup become available.
6. The protected root mounts once authentication and runtime readiness are true.
7. Logout or remote invalidation revokes the scope, increments the epoch, ignores stale results, stops protected work, and returns to the gate.

### Clean packaged execution

1. The packaged App validates and prepares only the bundled minimum authentication runtime.
2. The common login/session flow runs against `https://c2sml.cn/agent`.
3. After authentication, the package prepares the full Hermes/Voice runtime with the domestic-mirror-first policy and visible sanitized progress.
4. Payload and runtime integrity checks complete before `runtime_ready` becomes true.
5. The common Auth Guard mounts the protected product exactly once.
6. Logout tears down the protected epoch and returns to the login gate without uninstalling the verified runtime.

## Migration strategy

Use a clean-base reconstruction; never merge the existing macOS or Windows integration branches wholesale:

1. Reconfirm `ansatz/main` and every locked reference.
2. Replace the obsolete common-only boundary gate with an ownership manifest that permits the existing dual-platform skeleton and the reviewed cross-platform packaging foundation, while rejecting committed generated artifacts, credentials, CI-only product code, and unrelated release evidence.
3. Merge the reviewed original authentication foundation.
4. Replay every accepted authentication evolution in dependency order, splitting mixed commits and preserving final DMG behavior.
5. Import the complete Voice commit and resolve overlaps without losing authentication protection.
6. Reconcile the accepted Windows secure-store, named-pipe, process, and installer behavior behind the same contracts.
7. Promote the final DMG bootstrap, payload, progress, mirror, integrity, and repair design into cross-platform `main` implementations rather than leaving it macOS-only.
8. Reconcile `pyproject.toml`, `uv.lock`, `uv.toml`, npm locks, minimal-auth locks, and payload manifests reproducibly.
9. Repair packaging automation without introducing CI-only product behavior.
10. Prove source parity, product-file parity, artifact contents, clean installation, login, Voice, session restore, and logout on both platforms.

The implementation plan must be rewritten around this design before product replay resumes. The previous common-only plan is obsolete and must not be executed.

## Required proof gates

The candidate cannot be proposed for `main` until all gates pass:

1. **DMG product parity:** every accepted authentication and Voice path matches `80db6d8265`, except reviewed OS-neutral wording or Windows adapter changes with path-specific evidence.
2. **Entrypoint parity:** every non-installer protected entrypoint in the final DMG remains protected in `main` and in both installed products.
3. **Recursive mirror policy:** authentication bootstrap, full runtime, repair, update, optional feature, Voice model, and lazy-dependency tests prove domestic-first ordering, bounded fallback, hash validation, child-process propagation, environment sanitization, and recoverable failure. A network-origin inventory and controlled-proxy test prove that no Hermes-managed nested installer contacts an unregistered or official-first package source.
4. **macOS clean artifact:** `dist:mac:dmg` produces a real DMG; a clean-state installation can log in, prepare runtime, use Voice, restore a valid session online, log out, and relock.
5. **Windows clean artifact:** `dist:win:nsis` produces a real NSIS installer; a clean Windows installation passes the same behavior using Credential Manager and named pipes.
6. **Pre-auth isolation:** no backend, Agent, gateway, protected HTTP/WS listener, protected root, or protected IPC starts before authentication.
7. **Package contents:** required auth/runtime resources, hashes, native architecture files, icons, and metadata are present; `.github`, tests, credentials, sessions, logs, and source-only evidence are absent.
8. **Voice parity:** the accepted Voice paths remain blob-equivalent to `3ad4a12660` unless an authentication integration change is documented and behaviorally tested.
9. **Account isolation:** logout and rapid account switching cannot publish or accept work from an earlier runtime epoch.
10. **Distribution security:** macOS signing/notarization and Windows installer/signing status are recorded independently from functional correctness; unsigned local test artifacts are never represented as distributable production artifacts.

## Failure and security invariants

- Every missing dependency, credential-store error, bridge failure, timeout, network error, malformed progress event, owner race, corrupt payload, or architecture mismatch remains locked.
- Retry repeats only the failed safe operation and never reuses a password.
- Raw installer output never reaches the renderer.
- Protected IPC stays fail-closed during bootstrap and teardown.
- Voice failures never become authentication failures and never open an alternate service path.
- Platform adapters cannot override auth scope or runtime epoch decisions.
- Logs, workflows, artifacts, and evidence never include credentials or session material.
- A mirror outage produces a bounded, understandable failure with Retry; it never produces an infinite spinner or a partially authorized product.

## Intended branch graph

```text
ansatz/main
└── common authentication + Voice/SenseVoice
    + macOS/Windows clean-install packaging foundation
    + domestic-mirror-first bootstrap/runtime policy
    ├── macOS release operation
    │   └── credentials, notarization execution, artifact publication, evidence
    └── Windows release operation
        └── credentials, signing execution, artifact publication, evidence
```

The platform release operations may use branches or tags for controlled publication, but they must not carry a forked copy of authentication, Voice, bootstrap policy, or installer behavior. The existing DMG and Windows branches remain immutable behavior and artifact references until replacements built from the new `main` pass clean-machine acceptance.

This design authorizes work only on `integration/main-auth-voice-base`. It does not authorize a merge or push to `main`.
