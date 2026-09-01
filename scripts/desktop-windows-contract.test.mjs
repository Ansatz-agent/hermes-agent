import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { exeIdentityOptions } from '../apps/desktop/scripts/exe-identity-options.mjs'
import {
  assertX64HermesExecutable,
  findNewestWindowsNsis,
  forbiddenBrowserDownloadLine,
  readPeMachine
} from './desktop-windows-contract.mjs'

function writePe(filePath, machine = 0x8664) {
  const buffer = Buffer.alloc(256)
  buffer.write('MZ', 0, 'ascii')
  buffer.writeUInt32LE(128, 0x3c)
  buffer.write('PE\0\0', 128, 'binary')
  buffer.writeUInt16LE(machine, 132)
  fs.writeFileSync(filePath, buffer)
}

test('readPeMachine recognizes an x64 PE executable', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-win-pe-'))
  try {
    const exe = path.join(root, 'Ansatz.exe')
    writePe(exe)
    assert.equal(readPeMachine(exe), 0x8664)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('readPeMachine rejects a file without a PE header', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-win-pe-'))
  try {
    const exe = path.join(root, 'Ansatz.exe')
    fs.writeFileSync(exe, 'not a PE')
    assert.throws(() => readPeMachine(exe), /not a valid PE executable/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('findNewestWindowsNsis selects a fresh Windows x64 installer', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-win-release-'))
  try {
    const stale = path.join(root, 'Ansatz-0.16.0-win-x64.exe')
    const current = path.join(root, 'Ansatz-0.17.0-win-x64.exe')
    writePe(stale)
    // NSIS installer-stub bitness does not define the bundled app's bitness.
    writePe(current, 0x014c)
    fs.utimesSync(stale, new Date(1_000), new Date(1_000))
    fs.utimesSync(current, new Date(3_000), new Date(3_000))
    assert.equal(findNewestWindowsNsis(root, 2_000), current)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('findNewestWindowsNsis rejects stale, malformed, and MSI artifacts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-win-release-'))
  try {
    const stale = path.join(root, 'Ansatz-0.17.0-win-x64.exe')
    const malformed = path.join(root, 'Ansatz-0.18.0-win-x64.exe')
    const msi = path.join(root, 'Ansatz-0.18.0-win-x64.msi')
    writePe(stale)
    fs.writeFileSync(malformed, 'not a PE')
    writePe(msi)
    fs.utimesSync(stale, new Date(1_000), new Date(1_000))
    fs.utimesSync(malformed, new Date(3_000), new Date(3_000))
    fs.utimesSync(msi, new Date(3_000), new Date(3_000))
    assert.throws(
      () => findNewestWindowsNsis(root, 2_000),
      /no current Windows x64 Ansatz NSIS installer found/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('assertX64HermesExecutable validates the packaged application, not the installer stub', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-win-release-'))
  try {
    const exe = path.join(root, 'win-unpacked', 'Ansatz.exe')
    fs.mkdirSync(path.dirname(exe), { recursive: true })
    writePe(exe)
    assert.equal(assertX64HermesExecutable(root), exe)
    writePe(exe, 0x014c)
    assert.throws(() => assertX64HermesExecutable(root), /must be x64/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('Windows packaging rejects Playwright-managed browser downloads', () => {
  assert.equal(
    forbiddenBrowserDownloadLine('before\nDownloading Chromium 145.0.0 (playwright build v1208)\nafter'),
    'Downloading Chromium 145.0.0 (playwright build v1208)'
  )
  assert.equal(forbiddenBrowserDownloadLine('downloading electron-v40.10.2-win32-x64.zip'), null)
})

test('Windows npm timeout runner waits for a stable native exit code', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const helperStart = installer.indexOf('function _Invoke-NativeWithTimeout')
  const helperEnd = installer.indexOf('function _Run-NpmInstall', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart, 'missing npm timeout helper')

  const helper = installer.slice(helperStart, helperEnd)
  assert.match(helper, /while \(-not \$proc\.WaitForExit\(750\)\)/)
  assert.match(helper, /\$nextHeartbeat = \[DateTime\]::UtcNow\.AddSeconds\(30\)/)
  assert.match(helper, /\[hermes\] command still running/)
  assert.match(helper, /\$proc\.WaitForExit\(\)[\s\S]*\$proc\.Refresh\(\)[\s\S]*return \[int\]\$proc\.ExitCode/)
  assert.doesNotMatch(helper, /while \(-not \$proc\.HasExited\)/)
})

test('Windows installer keeps every machine-readable mode free of path diagnostics', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const modeStart = installer.indexOf('$script:MachineReadableMode =')
  const modeEnd = installer.indexOf('\n', modeStart)
  assert.ok(modeStart >= 0 && modeEnd > modeStart, 'missing machine-readable mode guard')

  const modeGuard = installer.slice(modeStart, modeEnd)
  for (const mode of ['$ProtocolVersion', '$ShowResolvedPaths', '$Manifest', '$Json']) {
    assert.match(modeGuard, new RegExp(`\\${mode}`))
  }
  assert.match(modeGuard, /\$PSBoundParameters\.ContainsKey\(['"]Stage['"]\)/)

  const helperStart = installer.indexOf('function Write-PathDiag')
  const helperEnd = installer.indexOf('function Get-LongProfileRoot', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart, 'missing path diagnostic helper')
  assert.match(installer.slice(helperStart, helperEnd), /if \(\$script:MachineReadableMode\) \{ return \}/)
})

test('Windows Python bootstrap isolates uv stderr from PowerShell error records', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const testPythonStart = installer.indexOf('function Test-Python')
  const testPythonEnd = installer.indexOf('function Install-Git', testPythonStart)
  assert.ok(testPythonStart >= 0 && testPythonEnd > testPythonStart, 'missing Test-Python')

  const testPython = installer.slice(testPythonStart, testPythonEnd)
  assert.match(testPython, /Start-Process -FilePath \$UvCmd/)
  assert.match(testPython, /-ArgumentList @\("--no-config", "python", "install", \$PythonVersion\)/)
  assert.match(testPython, /-RedirectStandardOutput \$uvStdoutLog/)
  assert.match(testPython, /-RedirectStandardError \$uvStderrLog/)
  assert.doesNotMatch(testPython, /& \$UvCmd python install \$PythonVersion 2>&1/)
  assert.doesNotMatch(installer, /& \$UvCmd(?! --no-config)[^\r\n]*python find/)
})

test('Windows install-method stamp is idempotently replaced with a valid backup path', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const helperStart = installer.indexOf('function Write-InstallMethod')
  const helperEnd = installer.indexOf('function Install-Repository', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart, 'missing install-method writer')

  const helper = installer.slice(helperStart, helperEnd)
  assert.match(helper, /\$backupPath = "\$methodPath\.backup-/)
  assert.match(helper, /\[System\.IO\.File\]::Replace\(\$temporaryPath, \$methodPath, \$backupPath\)/)
  assert.match(helper, /Remove-Item -LiteralPath \$temporaryPath, \$backupPath/)
  assert.doesNotMatch(helper, /\[System\.IO\.File\]::Replace\(\$temporaryPath, \$methodPath, \$null\)/)
})

test('Windows venv recreation never terminates the desktop shell by executable name', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const cleanupStart = installer.indexOf('Stopping Hermes runtime processes before recreating venv')
  const cleanupEnd = installer.indexOf('# Move the old venv aside', cleanupStart)
  assert.ok(cleanupStart >= 0 && cleanupEnd > cleanupStart, 'missing scoped Windows venv cleanup')

  const cleanup = installer.slice(cleanupStart, cleanupEnd)
  assert.doesNotMatch(cleanup, /taskkill[^\r\n]*\/IM\s+hermes\.exe/i)
  assert.match(cleanup, /Get-CimInstance Win32_Process/)
  assert.match(cleanup, /\.ExecutablePath\.StartsWith\(\$venvPrefix/)
  assert.match(cleanup, /taskkill \/F \/T \/PID \$treePid/)
})

test('Windows auth runtime update is exact-owner scoped and transactional', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const authStart = installer.indexOf('function Get-PendingAuthVenvTransaction')
  const authEnd = installer.indexOf('function Install-Dependencies', authStart)
  assert.ok(authStart >= 0 && authEnd > authStart, 'missing auth-venv transaction helpers')

  const auth = installer.slice(authStart, authEnd)
  for (const helper of [
    'Get-PendingAuthVenvTransaction',
    'Restore-AuthVenvTransaction',
    'Complete-AuthVenvTransaction',
    'Stop-ExactAuthRuntimeOwner'
  ]) {
    assert.match(auth, new RegExp(`function ${helper}`), `missing ${helper}`)
  }
  assert.match(auth, /auth-venv\.pending-backup/)
  assert.match(auth, /auth-venv\.stale\./)
  assert.match(auth, /markerExisted/)
  assert.match(auth, /markerBase64/)
  assert.match(auth, /GetOwnerSid/)
  assert.match(auth, /\.ExecutablePath/)
  assert.match(auth, /hermes_cli\.client_auth\.runtime owner/)
  assert.match(auth, /\[System\.StringComparison\]::OrdinalIgnoreCase/)
  assert.match(auth, /Authentication owner candidate identity is incomplete/)
  assert.match(auth, /Authentication owner SID could not be confirmed/)
  assert.doesNotMatch(auth, /Get-Process\s+(?:python|pythonw)/i)
  assert.doesNotMatch(auth, /taskkill[^\r\n]*\/IM\s+python/i)

  const installAuthVenvStart = installer.indexOf('function Install-AuthVenv')
  const installAuthVenvEnd = installer.indexOf('function Complete-VenvTransaction', installAuthVenvStart)
  const installAuthVenv = installer.slice(installAuthVenvStart, installAuthVenvEnd)
  assert.match(installAuthVenv, /Restore-AuthVenvTransaction/)
  assert.match(installAuthVenv, /Stop-ExactAuthRuntimeOwner/)
  assert.match(installAuthVenv, /auth-venv\.stale\./)
  assert.match(installAuthVenv, /previous authentication marker is too large/i)
  assert.match(installAuthVenv, /Rename-Item -LiteralPath \$authVenv -NewName \$backupName/)
  assert.doesNotMatch(installAuthVenv, /Authentication virtual environment already exists[\s\S]*return/)
  assert.ok(
    installAuthVenv.indexOf('Stop-ExactAuthRuntimeOwner') <
      installAuthVenv.indexOf('Resolve-AvailablePythonVersion'),
    'owner retirement must happen before interpreter resolution'
  )
  assert.ok(
    installAuthVenv.indexOf('Restore-AuthVenvTransaction') <
      installAuthVenv.indexOf('Resolve-AvailablePythonVersion'),
    'pending auth transaction recovery must happen before interpreter resolution'
  )
  assert.ok(
    installAuthVenv.indexOf('previous authentication marker is too large') <
      installAuthVenv.indexOf('Write-AtomicAuthJson'),
    'oversized marker rejection must happen before publishing the transaction record'
  )
  assert.match(auth, /Authentication owner candidate limit exceeded/)

  const authDependenciesStart = installer.indexOf('function Install-AuthDependencies')
  const authDependenciesEnd = installer.indexOf('function Install-Dependencies', authDependenciesStart)
  const authDependencies = installer.slice(authDependenciesStart, authDependenciesEnd)
  assert.match(authDependencies, /\$UvCmd sync[\s\S]*--locked[\s\S]*--no-install-project/)
  assert.match(authDependencies, /hermes_cli\.client_auth\.bridge/)
  assert.match(authDependencies, /PROTOCOL_VERSION/)
  assert.match(authDependencies, /Restore-AuthVenvTransaction/)

  const authCompleteStart = installer.indexOf('function Write-AuthBootstrapComplete')
  const authCompleteEnd = installer.indexOf('function Write-BootstrapMarker', authCompleteStart)
  const authComplete = installer.slice(authCompleteStart, authCompleteEnd)
  assert.match(authComplete, /schemaVersion\s*=\s*2/)
  for (const field of ['sourceCommit', 'sourceArchiveSha256', 'authLockSha256', 'protocolVersion']) {
    assert.match(authComplete, new RegExp(field))
  }
  assert.match(authComplete, /Complete-AuthVenvTransaction/)
  assert.match(authComplete, /Restore-AuthVenvTransaction/)
})

test('Windows installer receives managed uv from the packaged toolchain', () => {
  const installer = fs.readFileSync(path.resolve(import.meta.dirname, 'install.ps1'), 'utf8')
  const installUvStart = installer.indexOf('function Install-Uv')
  const installUvEnd = installer.indexOf('function Sync-EnvPath', installUvStart)
  const installUv = installer.slice(installUvStart, installUvEnd)
  assert.ok(installUvStart >= 0 && installUvEnd > installUvStart, 'missing managed uv installer')
  assert.match(installUv, /Hermes owns its own uv/)
  assert.match(installUv, /BundledToolchain/)
  assert.match(installUv, /bundledUv = Join-Path \$BundledToolchain "uv\.exe"/)
  assert.match(installUv, /Managed uv adopted from the bundled toolchain/)
  assert.match(installUv, /Get-Command uv/)
  assert.doesNotMatch(installUv, /Invoke-WebRequest|irm\b|iex\b|github\.com\/astral-sh/)

  const toolchain = fs.readFileSync(
    path.resolve(import.meta.dirname, '..', 'apps', 'desktop', 'electron', 'package-runtime', 'windows-auth-toolchain.ts'),
    'utf8'
  )
  assert.match(toolchain, /fs\.copyFileSync\(toolchain\.uvAssetPath, uvExecutable\)/)
  assert.match(toolchain, /'--no-index'/)
  assert.match(toolchain, /'--require-hashes'/)

  const desktopPackage = fs.readFileSync(
    path.resolve(import.meta.dirname, '..', 'apps', 'desktop', 'package.json'),
    'utf8'
  )
  assert.match(desktopPackage, /auth-toolchain/)
})

test('Windows product scripts expose native package and installed-auth verification', () => {
  const rootPackage = JSON.parse(
    fs.readFileSync(path.resolve(import.meta.dirname, '..', 'package.json'), 'utf8')
  )
  assert.equal(rootPackage.scripts['build:desktop:windows'], 'node scripts/build-desktop-windows.mjs')
  assert.equal(rootPackage.scripts['package:desktop:windows'], 'node scripts/package-desktop-windows.mjs')
  assert.match(rootPackage.scripts['test:desktop:windows-contract'], /desktop-windows-contract\.test\.mjs/)
  assert.match(rootPackage.scripts['test:desktop:windows-contract'], /desktop-credential-login\.test\.mjs/)
  assert.match(rootPackage.scripts['test:desktop:windows-contract'], /package-desktop-windows\.test\.mjs/)

  const hostHarness = fs.readFileSync(
    path.resolve(import.meta.dirname, 'test-desktop-windows-auth-host.ps1'),
    'utf8'
  )
  assert.match(hostHarness, /test-desktop-windows-install\.ps1/)
  assert.match(hostHarness, /-RunAuthE2E/)
  assert.match(hostHarness, /-AuthLogPath \$AuthLogPath/)

  const installHarness = fs.readFileSync(
    path.resolve(import.meta.dirname, 'test-desktop-windows-install.ps1'),
    'utf8'
  )
  assert.match(installHarness, /test:e2e:installed-windows-smoke/)
  assert.match(installHarness, /test:e2e:installed-windows-auth/)
  assert.match(installHarness, /desktop-credential-login\.mjs/)
  assert.match(installHarness, /\$ProductName\s*=\s*'Ansatz'/)
  assert.match(installHarness, /\$ExecutableName\s*=\s*'Ansatz'/)
  assert.match(installHarness, /\$ExecutableName\.exe/)
  assert.match(installHarness, /Uninstall \$ProductName\.exe/)
  assert.match(installHarness, /RedirectStandardInput\s*=\s*\$true/)
  assert.match(installHarness, /Remove-Item -LiteralPath "Env:HERMES_E2E_USERNAME"/)
  assert.match(installHarness, /Remove-Item -LiteralPath "Env:HERMES_E2E_PASSWORD"/)
  assert.doesNotMatch(
    installHarness,
    /ArgumentList[^\r\n]*(?:E2E_USERNAME|E2E_PASSWORD|credentialUsername|credentialPassword)/
  )
})

test('Windows installed-auth test preserves hard-gate and runtime evidence', () => {
  const authAssertions = fs.readFileSync(
    path.resolve(import.meta.dirname, '..', 'apps', 'desktop', 'e2e', 'auth-assertions.ts'),
    'utf8'
  )
  assert.match(authAssertions, /export function trackProtectedRendererChunk/)
  assert.match(authAssertions, /page\.on\('request'/)
  assert.doesNotMatch(authAssertions, /performance\.getEntriesByType\('resource'\)/)

  const installedAuth = fs.readFileSync(
    path.resolve(import.meta.dirname, '..', 'apps', 'desktop', 'e2e', 'installed-windows-auth.spec.ts'),
    'utf8'
  )
  for (const contract of [
    /trackProtectedRendererChunk/,
    /\[data-slot="statusbar"\]/,
    /\.hermes-auth-bootstrap-complete/,
    /auth-upgrade-rollback-sentinel/,
    /auth-venv\.pending-backup/,
    /sourceArchiveSha256/,
    /authLockSha256/,
    /protocolVersion/,
    /expectFullRuntimeAbsent\(activeRoot\)/,
    /backend-ownership\.json/,
    /assertInstalledPayloadBoundary\(activeRoot\)/
  ]) {
    assert.match(installedAuth, contract)
  }
  assert.match(installedAuth, /waitForInstalledAuthRuntime\(page, activeRoot, authVenvPython, 31 \* 60_000\)/)
  assert.match(installedAuth, /waitForInstalledFullRuntime\(page, activeRoot, venvPython, 31 \* 60_000\)/)
  assert.match(installedAuth, /Hermes could not prepare its local runtime\./)

  const bootstrapRunner = fs.readFileSync(
    path.resolve(import.meta.dirname, '..', 'apps', 'desktop', 'electron', 'bootstrap-runner.ts'),
    'utf8'
  )
  assert.match(bootstrapRunner, /const DEFAULT_BOOTSTRAP_TIMEOUTS = Object\.freeze/)
  assert.match(bootstrapRunner, /idleMs: 90_000/)
  assert.match(bootstrapRunner, /progressHeartbeatMsForStage/)
})

test('Windows executable identity stamps the Ansatz app version, not the Electron runtime version', () => {
  const options = exeIdentityOptions({ icon: 'Ansatz.ico', productVersion: '0.17.0' })
  assert.equal(options['file-version'], '0.17.0')
  assert.equal(options['product-version'], '0.17.0')
  assert.equal(options['version-string'].ProductName, 'Ansatz')
  assert.equal(options['version-string'].FileDescription, 'Ansatz')
})
