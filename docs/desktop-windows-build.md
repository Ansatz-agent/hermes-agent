# Build the Ansatz Windows installer

The supported local Windows packaging entry point is:

```powershell
npm run package:desktop:windows
```

It builds the x64 NSIS installer from the locked repository toolchain and runs
the same gates used by the packaging workflow:

1. verify Windows x64 and the pinned Node.js version from `.node-version`;
2. install the root dependency tree with `npm ci`;
3. run the Windows product contracts and Desktop TypeScript typecheck;
4. prepare the offline bootstrap payload, including the hash-verified Git Bash
   runtime shipped with the installer;
5. build the NSIS installer and validate the unpacked x64 executable;
6. audit `app.asar`, the backend archive, manifests, and payload hashes;
7. install the generated NSIS package into an isolated temporary profile and
   run the clean-install smoke test.

The smoke test is important for fresh machines: it launches the installed app
with no existing Hermes runtime and verifies that the product-scoped
`%LOCALAPPDATA%\AnsatzVoiceTraceClient\git` runtime is found after bootstrap.

## Preflight

To check the host without installing dependencies or producing an artifact:

```powershell
npm run package:desktop:windows -- --check
```

## Outputs

- Installer: `apps/desktop/release/Ansatz-<version>-win-x64.exe`
- Build log: `apps/desktop/build/logs/desktop-windows-package.log`
- Install smoke log: `apps/desktop/build/logs/desktop-windows-install-smoke.log`
- Sanitized report: `apps/desktop/build/reports/windows-package.json`

For a fast package-only rerun when a Windows installation test is not needed,
use `npm run package:desktop:windows -- --skip-install-smoke`. The default
flow should be used for release candidates and clean-machine verification.

The generated package is unsigned unless a separate signing configuration is
provided. A successful local build proves packaging integrity; Windows-native
credential/login and uninstall acceptance still belong in the release CI
workflow.
