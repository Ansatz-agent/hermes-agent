import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { protectedIpcRejections, trackProtectedRendererChunk } from './auth-assertions'
import { type FixedAuthContractServer, startFixedAuthContractServer } from './fixed-auth-contract-server'
import {
  closeDesktopApp,
  createSandbox,
  launchInstalledWindowsApp,
  resolveInstalledWindowsBinary,
  type Sandbox
} from './fixtures'
import { allowErrorBanners, type ElectronApplication, expect, type Page, test } from './test'
import {
  capturedProcessIdsDeepestFirst,
  descendantsOf,
  windowsPathAliases,
  type WindowsProcessRow,
  windowsProcessSnapshot
} from './windows-process-tree'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')
const AUTH_BRIDGE_COMMAND = /hermes_cli\.client_auth\.bridge\b/i
const AUTH_OWNER_COMMAND = /hermes_cli\.client_auth\.runtime(?:"|')?\s+owner\b/i
const KEYRING_SERVICE = 'cn.c2sml.hermes.remote-auth'
const KEYRING_ACCOUNT = 'django-session'

const BOOTSTRAP_LOG_ARTIFACT = path.join(
  REPO_ROOT,
  'apps',
  'desktop',
  'build',
  'logs',
  'phase1-desktop-windows-nsis-bootstrap.log'
)

function redactBootstrapDiagnostics(contents: string): string {
  return contents
    .replace(/((?:authorization|cookie|set-cookie)\s*:\s*)\S+/gi, '$1[REDACTED]')
    .replace(/((?:password|passwd|session|sessionid|csrf|csrftoken|bearer|keychain)\s*[=:]\s*)\S+/gi, '$1[REDACTED]')
}

function preserveBootstrapDiagnostics(hermesHome: string): void {
  const logRoot = path.join(hermesHome, 'logs')

  if (!fs.existsSync(logRoot)) {
    return
  }

  const logs = fs
    .readdirSync(logRoot)
    .filter(name => /^bootstrap-.*\.log$/i.test(name))
    .sort()
    .map(name => `===== ${name} =====\n${fs.readFileSync(path.join(logRoot, name), 'utf8')}`)

  if (logs.length > 0) {
    fs.mkdirSync(path.dirname(BOOTSTRAP_LOG_ARTIFACT), { recursive: true })
    fs.writeFileSync(BOOTSTRAP_LOG_ARTIFACT, redactBootstrapDiagnostics(logs.join('\n')), 'utf8')
  }
}

function assertBootstrapNodeDependenciesSucceeded(hermesHome: string): void {
  const logRoot = path.join(hermesHome, 'logs')

  const contents = fs
    .readdirSync(logRoot)
    .filter(name => /^bootstrap-.*\.log$/i.test(name))
    .map(name => fs.readFileSync(path.join(logRoot, name), 'utf8'))
    .join('\n')

  expect(contents).toContain('[OK] Browser tools dependencies installed')
  expect(contents).toContain('[OK] Playwright Chromium installed (browser tools ready)')
  expect(contents).toContain('[OK] TUI dependencies installed')
  expect(contents).not.toMatch(/(?:Browser tools|TUI) npm install (?:failed|timed out|could not be launched)/i)
}

interface SafeBootstrapState {
  status: 'idle' | 'preparing' | 'complete' | 'failed'
  scope: 'auth' | 'runtime' | null
  generation: number
  revision: number
  stage: string | null
  completed: number
  total: number | null
  unit: 'bytes' | 'packages' | 'items' | 'files' | 'steps' | null
  error: 'bootstrap_failed' | null
}

interface InstalledAccountStatus {
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
  cloud_state: 'active' | 'unreachable' | 'reauth_required' | null
  validation_state: 'unknown' | 'validating' | 'online' | 'degraded'
  validation_reason: string | null
  runtime_ready: boolean
}

async function safeBootstrapState(page: Page): Promise<SafeBootstrapState> {
  return page.evaluate(() =>
    (
      window as unknown as {
        hermesDesktop: { authBootstrap: { getState: () => Promise<SafeBootstrapState> } }
      }
    ).hermesDesktop.authBootstrap.getState()
  )
}

async function installedAccountStatus(page: Page): Promise<InstalledAccountStatus> {
  return page.evaluate(() =>
    (
      window as unknown as {
        hermesDesktop: { auth: { status: () => Promise<InstalledAccountStatus> } }
      }
    ).hermesDesktop.auth.status()
  )
}

function expectFullRuntimeAbsent(activeRoot: string): void {
  expect(fs.existsSync(path.join(activeRoot, '.hermes-bootstrap-complete'))).toBe(false)
  expect(fs.existsSync(path.join(activeRoot, 'venv'))).toBe(false)
}

async function waitForInstalledAuthRuntime(
  page: Page,
  activeRoot: string,
  authVenvPython: string,
  timeoutMs: number
): Promise<void> {
  const authRuntimePath = path.join(activeRoot, 'hermes_cli', 'client_auth', 'runtime.py')
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    const state = await safeBootstrapState(page)

    if (state.status === 'failed') {
      throw new Error(`Installed Windows auth bootstrap failed: ${state.error}`)
    }

    if (
      state.status === 'complete' &&
      state.scope === 'auth' &&
      fs.existsSync(authVenvPython) &&
      fs.existsSync(authRuntimePath) &&
      fs.existsSync(path.join(activeRoot, '.hermes-auth-bootstrap-complete')) &&
      fs.existsSync(path.join(activeRoot, 'bin', 'hermes.cmd'))
    ) {
      expectFullRuntimeAbsent(activeRoot)

      return
    }

    await page.waitForTimeout(1_000)
  }

  throw new Error(`Installed Windows auth runtime was not ready within ${timeoutMs}ms`)
}

async function waitForInstalledFullRuntime(
  page: Page,
  activeRoot: string,
  venvPython: string,
  timeoutMs: number
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let previous: SafeBootstrapState | null = null

  while (Date.now() < deadline) {
    const state = await safeBootstrapState(page)

    if (state.status === 'failed') {
      throw new Error(`Installed Windows full runtime bootstrap failed: ${state.error}`)
    }

    if (previous && state.scope === 'runtime' && previous.scope === 'runtime') {
      expect(state.generation).toBeGreaterThanOrEqual(previous.generation)

      if (state.generation === previous.generation) {
        expect(state.revision).toBeGreaterThanOrEqual(previous.revision)

        if (state.stage === previous.stage && state.unit === previous.unit) {
          expect(state.completed).toBeGreaterThanOrEqual(previous.completed)

          if (previous.total !== null && state.total !== null) {
            expect(state.total).toBeGreaterThanOrEqual(previous.total)
          }
        }
      }
    }

    previous = state

    if (
      state.status === 'complete' &&
      state.scope === 'runtime' &&
      fs.existsSync(path.join(activeRoot, '.hermes-bootstrap-complete')) &&
      fs.existsSync(venvPython) &&
      fs.existsSync(path.join(activeRoot, 'bin', 'hermes.exe'))
    ) {
      expect(fs.existsSync(path.join(activeRoot, 'bin', 'hermes.cmd'))).toBe(false)

      return
    }

    await page.waitForTimeout(1_000)
  }

  throw new Error(`Installed Windows full runtime was not ready within ${timeoutMs}ms`)
}

function keyringRecordExists(pythonPath: string): boolean {
  const output = execFileSync(pythonPath, ['-'], {
    encoding: 'utf8',
    input: `import keyring
value = keyring.get_password(${JSON.stringify(KEYRING_SERVICE)}, ${JSON.stringify(KEYRING_ACCOUNT)})
print("1" if value is not None else "0")
`
  }).trim()

  if (output !== '0' && output !== '1') {
    throw new Error('Keyring presence probe returned an invalid result')
  }

  return output === '1'
}

function installedRuntimeProtocols(pythonPath: string, activeRoot: string): string {
  return execFileSync(
    pythonPath,
    [
      '-I',
      '-c',
      'import hermes_cli.client_auth.bridge as bridge; from hermes_cli.client_auth.backend_scope_protocol import DESKTOP_SCOPE_PROTOCOL_VERSION; print(f"{bridge.PROTOCOL_VERSION}:{DESKTOP_SCOPE_PROTOCOL_VERSION}")'
    ],
    { cwd: activeRoot, encoding: 'utf8' }
  ).trim()
}

interface BackendOwnershipEntry {
  command: string
  pid: number
}

function readBackendOwnership(userDataDir: string): BackendOwnershipEntry[] {
  const ownershipPath = path.join(userDataDir, 'backend-ownership.json')
  const parsed = JSON.parse(fs.readFileSync(ownershipPath, 'utf8')) as { backends?: unknown }

  if (!Array.isArray(parsed.backends)) {
    throw new Error('Installed Desktop backend ownership store is invalid')
  }

  return parsed.backends.map((value, index) => {
    if (
      !value ||
      typeof value !== 'object' ||
      !Number.isInteger((value as { pid?: unknown }).pid) ||
      typeof (value as { command?: unknown }).command !== 'string'
    ) {
      throw new Error(`Installed Desktop backend ownership entry ${index} is invalid`)
    }

    return value as BackendOwnershipEntry
  })
}

function backendDescendants(rootPid: number): WindowsProcessRow[] {
  return descendantsOf(windowsProcessSnapshot(), rootPid).filter(row =>
    /(?:hermes_cli\.main.*\b(?:serve|dashboard)\b|hermes(?:\.exe)?(?:"|')?\s+(?:serve|dashboard)\b)/i.test(
      row.commandLine
    )
  )
}

function authBridgeDescendants(rootPid: number): WindowsProcessRow[] {
  return descendantsOf(windowsProcessSnapshot(), rootPid).filter(row => AUTH_BRIDGE_COMMAND.test(row.commandLine))
}

function authBridgeRoots(bridgeProcesses: WindowsProcessRow[]): WindowsProcessRow[] {
  const bridgePids = new Set(bridgeProcesses.map(row => row.pid))

  return bridgeProcesses.filter(row => !bridgePids.has(row.parentPid))
}

function runtimeBootstrapDescendants(rootPid: number): WindowsProcessRow[] {
  return descendantsOf(windowsProcessSnapshot(), rootPid).filter(
    row => /install\.ps1/i.test(row.commandLine) && /-BootstrapScope\s+runtime\b/i.test(row.commandLine)
  )
}

function elapsedSeconds(label: string): number {
  const match = label.match(/(\d+)(?::(\d{2}))?(?::(\d{2}))?s?\s*$/)

  if (!match) {
    throw new Error(`Elapsed label has an unsupported format: ${label}`)
  }

  if (match[3] !== undefined) {
    return Number(match[1]) * 3_600 + Number(match[2]) * 60 + Number(match[3])
  }

  if (match[2] !== undefined) {
    return Number(match[1]) * 60 + Number(match[2])
  }

  return Number(match[1])
}

function assertInstalledPayloadBoundary(activeRoot: string): void {
  for (const required of [
    'desktop_auth_runtime/pyproject.toml',
    'desktop_auth_runtime/uv.lock',
    'desktop_auth_runtime/uv.toml',
    'hermes_cli/client_auth/cli.py',
    'hermes_cli/client_auth/bridge.py',
    'hermes_cli/client_auth/backend_scope_protocol.py'
  ]) {
    expect(fs.existsSync(path.join(activeRoot, ...required.split('/'))), `missing installed ${required}`).toBe(true)
  }

  for (const forbidden of [
    '.github',
    'tests',
    'tests-js',
    'apps/desktop',
    'apps/bootstrap-installer',
    'scripts/install_psutil_android.py'
  ]) {
    expect(fs.existsSync(path.join(activeRoot, ...forbidden.split('/'))), `unexpected installed ${forbidden}`).toBe(
      false
    )
  }

  const scripts = fs.readdirSync(path.join(activeRoot, 'scripts'))
  expect(scripts.filter(name => /^(?:desktop-dmg-|verify-desktop-dmg-)/i.test(name))).toEqual([])
}

function newAuthOwnerPids(initialOwnerPids: ReadonlySet<number>): number[] {
  return windowsProcessSnapshot()
    .filter(row => AUTH_OWNER_COMMAND.test(row.commandLine) && !initialOwnerPids.has(row.pid))
    .map(row => row.pid)
}

async function stopNewAuthOwnerProcesses(initialOwnerPids: ReadonlySet<number>): Promise<void> {
  for (const pid of newAuthOwnerPids(initialOwnerPids)) {
    try {
      execFileSync('powershell.exe', [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `Stop-Process -Id ${pid} -Force -ErrorAction SilentlyContinue`
      ])
    } catch {
      // The owner can exit between the process snapshot and Stop-Process.
      // The poll below still fails if any test-owned process remains.
    }
  }

  await expect.poll(() => newAuthOwnerPids(initialOwnerPids), { timeout: 10_000, intervals: [250] }).toEqual([])
}

async function stopCapturedWindowsProcesses(capturedRows: readonly WindowsProcessRow[]): Promise<number[]> {
  const stoppedPids = capturedProcessIdsDeepestFirst(capturedRows, windowsProcessSnapshot())

  for (const pid of stoppedPids) {
    try {
      execFileSync('powershell.exe', [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `Stop-Process -Id ${pid} -Force -ErrorAction SilentlyContinue`
      ])
    } catch {
      // The captured process can exit between the identity snapshot and Stop-Process.
      // The bounded identity poll below still fails if the original process remains.
    }
  }

  await expect
    .poll(() => capturedProcessIdsDeepestFirst(capturedRows, windowsProcessSnapshot()), {
      timeout: 10_000,
      intervals: [250]
    })
    .toEqual([])

  return stoppedPids
}

async function expectAuthenticated(page: Page, username: string, runtimeReady?: boolean): Promise<void> {
  await expect
    .poll(
      async () => {
        const status = await installedAccountStatus(page)

        return runtimeReady === undefined
          ? { state: status.state, username: status.username }
          : { state: status.state, username: status.username, runtime_ready: status.runtime_ready }
      },
      {
        timeout: 30_000,
        intervals: [250, 500, 1_000],
        message: 'authenticated status IPC did not settle after successful login'
      }
    )
    .toMatchObject({
      state: 'authenticated',
      username,
      ...(runtimeReady === undefined ? {} : { runtime_ready: runtimeReady })
    })
}

async function expectProtectedRendererChunk(requested: () => boolean): Promise<void> {
  await expect
    .poll(requested, {
      timeout: 30_000,
      intervals: [250, 500, 1_000],
      message: 'protected renderer chunk did not load after authentication'
    })
    .toBe(true)
}

test.describe.configure({ timeout: 60 * 60_000, retries: 0 })
test.use({ trace: 'off', video: 'off', screenshot: 'off' })

test('installed Windows Hermes enforces the complete account lifecycle', async () => {
  test.skip(process.platform !== 'win32', 'installed Windows auth lifecycle requires win32')
  allowErrorBanners()

  const requiredPathNames = [
    'HERMES_E2E_INSTALLED_BINARY',
    'HERMES_E2E_AUTH_CERT_PATH',
    'HERMES_E2E_AUTH_KEY_PATH',
    'HERMES_E2E_WRONG_SAN_CERT_PATH',
    'HERMES_E2E_WRONG_SAN_KEY_PATH'
  ] as const

  for (const name of requiredPathNames) {
    const value = process.env[name]

    if (!value || !path.isAbsolute(value)) {
      throw new Error(`${name} must be an absolute path`)
    }

    const stats = fs.lstatSync(value)

    if (!stats.isFile() || stats.isSymbolicLink()) {
      throw new Error(`${name} must name a regular file`)
    }
  }

  const executablePath = resolveInstalledWindowsBinary()
  const repoPython = path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')

  if (!fs.existsSync(repoPython) || !fs.lstatSync(repoPython).isFile()) {
    throw new Error('Repository .venv/Scripts/python.exe is required')
  }

  const repoCertifiBundle = execFileSync(repoPython, ['-c', 'import certifi; print(certifi.where())'], {
    encoding: 'utf8'
  }).trim()

  if (!path.isAbsolute(repoCertifiBundle) || !fs.existsSync(repoCertifiBundle)) {
    throw new Error('Repository certifi bundle is unavailable')
  }

  const repoCertifiHashBefore = createHash('sha256').update(fs.readFileSync(repoCertifiBundle)).digest('hex')

  if (keyringRecordExists(repoPython)) {
    throw new Error('Windows auth keyring record must be empty before the lifecycle test')
  }

  const initialOwnerPids = new Set(
    windowsProcessSnapshot()
      .filter(row => AUTH_OWNER_COMMAND.test(row.commandLine))
      .map(row => row.pid)
  )

  const sandbox: Sandbox = createSandbox('installed-windows-auth')
  const activeRoot = path.join(sandbox.hermesHome, 'hermes-agent')
  const authVenvRoot = path.join(activeRoot, 'auth-venv')
  const authVenvPython = path.join(authVenvRoot, 'python.exe')
  const authLockPath = path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock')
  const authMarkerPath = path.join(activeRoot, '.hermes-auth-bootstrap-complete')
  const authTransactionPath = path.join(activeRoot, 'auth-venv.pending-backup')
  const venvRoot = path.join(activeRoot, 'venv')
  const venvPython = path.join(venvRoot, 'Scripts', 'python.exe')
  const fullLockPath = path.join(activeRoot, 'uv.lock')

  if (fs.existsSync(authVenvRoot) || fs.existsSync(venvRoot)) {
    throw new Error('Installed auth sandbox unexpectedly contained a pre-existing environment')
  }

  let app: ElectronApplication | null = null
  let page: Page | null = null
  let server: FixedAuthContractServer | null = null
  let wrongSanServer: FixedAuthContractServer | null = null
  let savedFullLock: Buffer | null = null
  const generatedSensitiveValues: Array<{ category: string; value: string }> = []

  try {
    let launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    let protectedRendererChunkRequested = trackProtectedRendererChunk(page)
    expect(protectedRendererChunkRequested()).toBe(false)
    const bootstrapRootPid = app.process().pid
    expect(bootstrapRootPid).toBeDefined()
    expect(backendDescendants(bootstrapRootPid!)).toHaveLength(0)
    expectFullRuntimeAbsent(activeRoot)
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 10 * 60_000 })
    await waitForInstalledAuthRuntime(page, activeRoot, authVenvPython, 31 * 60_000)
    expect(protectedRendererChunkRequested()).toBe(false)
    expect(backendDescendants(bootstrapRootPid!)).toHaveLength(0)
    await closeDesktopApp(app, { timeoutMs: 10_000 })
    app = null
    page = null

    let upgradeOwnerPids: number[] = []
    await expect
      .poll(
        () => {
          upgradeOwnerPids = newAuthOwnerPids(initialOwnerPids)

          return upgradeOwnerPids.length
        },
        {
          timeout: 30_000,
          intervals: [250, 500],
          message: 'fresh auth runtime did not leave the detached owner needed for upgrade coverage'
        }
      )
      .toBeGreaterThan(0)

    const rollbackSentinel = path.join(authVenvRoot, 'auth-upgrade-rollback-sentinel')
    const legacyMarker = JSON.stringify({ schemaVersion: 1, scope: 'auth' })
    fs.writeFileSync(rollbackSentinel, 'previous-auth-runtime\n', 'utf8')
    fs.writeFileSync(authMarkerPath, legacyMarker, 'utf8')

    // A stale marker forces a pre-login authentication-runtime replacement.
    // The exact detached owner must be retired before auth-venv is published,
    // and the repaired schema-2 contract must be visible before login.

    launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    await waitForInstalledAuthRuntime(page, activeRoot, authVenvPython, 31 * 60_000)
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    await expect
      .poll(
        () => {
          const live = new Set(windowsProcessSnapshot().map(row => row.pid))

          return upgradeOwnerPids.filter(pid => live.has(pid))
        },
        {
          timeout: 30_000,
          intervals: [250, 500],
          message: 'old auth owner survived pre-login upgrade retirement'
        }
      )
      .toEqual([])

    const repairedMarker = JSON.parse(fs.readFileSync(authMarkerPath, 'utf8')) as Record<string, unknown>

    const sourceMarker = JSON.parse(
      fs.readFileSync(path.join(activeRoot, '.hermes-bundled-source.json'), 'utf8')
    ) as Record<string, unknown>

    expect(Object.keys(repairedMarker).sort()).toEqual([
      'authLockSha256',
      'protocolVersion',
      'schemaVersion',
      'scope',
      'sourceArchiveSha256',
      'sourceCommit'
    ])
    expect(repairedMarker).toMatchObject({
      schemaVersion: 2,
      scope: 'auth',
      sourceCommit: sourceMarker.commit,
      sourceArchiveSha256: sourceMarker.archiveSha256,
      authLockSha256: createHash('sha256').update(fs.readFileSync(authLockPath)).digest('hex'),
      protocolVersion: 2
    })
    expect(installedRuntimeProtocols(authVenvPython, activeRoot)).toBe('2:2')
    expect(fs.existsSync(authTransactionPath)).toBe(false)
    expect(fs.existsSync(rollbackSentinel)).toBe(false)
    expectFullRuntimeAbsent(activeRoot)
    await closeDesktopApp(app, { timeoutMs: 10_000 })
    app = null
    page = null

    await stopNewAuthOwnerProcesses(initialOwnerPids)

    const sandboxCertifiBundle = execFileSync(authVenvPython, ['-c', 'import certifi; print(certifi.where())'], {
      encoding: 'utf8'
    }).trim()

    const certifiRelative = path.relative(path.resolve(sandbox.hermesHome), path.resolve(sandboxCertifiBundle))

    if (
      !path.isAbsolute(sandboxCertifiBundle) ||
      certifiRelative.startsWith('..') ||
      path.isAbsolute(certifiRelative) ||
      !fs.existsSync(sandboxCertifiBundle)
    ) {
      throw new Error('Sandbox certifi bundle escaped the isolated HERMES_HOME')
    }

    for (const certPath of [process.env.HERMES_E2E_AUTH_CERT_PATH!, process.env.HERMES_E2E_WRONG_SAN_CERT_PATH!]) {
      fs.appendFileSync(sandboxCertifiBundle, `\n${fs.readFileSync(certPath, 'utf8').trim()}\n`, 'utf8')
    }

    expect(createHash('sha256').update(fs.readFileSync(repoCertifiBundle)).digest('hex')).toBe(repoCertifiHashBefore)

    wrongSanServer = await startFixedAuthContractServer({
      certPath: process.env.HERMES_E2E_WRONG_SAN_CERT_PATH!,
      keyPath: process.env.HERMES_E2E_WRONG_SAN_KEY_PATH!
    })

    for (const [index, value] of wrongSanServer.sensitiveValues().entries()) {
      generatedSensitiveValues.push({ category: `wrong-san-auth-${index}`, value })
    }

    launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    protectedRendererChunkRequested = trackProtectedRendererChunk(page)
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    await page.locator('input[name="username"]').fill(wrongSanServer.username)
    await page.locator('input[name="password"]').fill(wrongSanServer.password)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator('main section p[role="alert"]')).toBeVisible({ timeout: 30_000 })
    expect(wrongSanServer.events()).toHaveLength(0)
    expect(keyringRecordExists(authVenvPython)).toBe(false)
    expect(protectedRendererChunkRequested()).toBe(false)
    expectFullRuntimeAbsent(activeRoot)

    for (const rejection of await protectedIpcRejections(page)) {
      expect(rejection).toContain('AUTH_REQUIRED')
    }

    const wrongSanRootPid = app.process().pid
    expect(wrongSanRootPid).toBeDefined()
    expect(backendDescendants(wrongSanRootPid!)).toHaveLength(0)
    await closeDesktopApp(app, { timeoutMs: 10_000 })
    app = null
    page = null
    await wrongSanServer.close()
    wrongSanServer = null
    await stopNewAuthOwnerProcesses(initialOwnerPids)

    server = await startFixedAuthContractServer({
      certPath: process.env.HERMES_E2E_AUTH_CERT_PATH!,
      keyPath: process.env.HERMES_E2E_AUTH_KEY_PATH!
    })
    launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    protectedRendererChunkRequested = trackProtectedRendererChunk(page)
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('input[name="password"]')).toHaveAttribute('type', 'password')
    await expect(page.getByText('https://c2sml.cn/auth')).toBeVisible()
    await expect(page.locator('textarea, [contenteditable="true"], [data-terminal-slot]')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText('Setting up Hermes Agent')
    expect(protectedRendererChunkRequested()).toBe(false)

    for (const rejection of await protectedIpcRejections(page)) {
      expect(rejection).not.toBe('ALLOWED')
      expect(rejection).toContain('AUTH_REQUIRED')
      expect(rejection).not.toContain('sessionid')
    }

    const protectedWindows = await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().map((window: { webContents: { getURL: () => string } }) =>
        window.webContents.getURL()
      )
    )

    expect(protectedWindows).toHaveLength(1)
    expect(protectedWindows[0]).not.toMatch(/[?&]win=(?:hud|overlay|quick|wake)(?:&|$)/)
    const lockedRootPid = app.process().pid
    expect(lockedRootPid).toBeDefined()
    expect(backendDescendants(lockedRootPid!)).toHaveLength(0)
    expectFullRuntimeAbsent(activeRoot)

    await page.locator('input[name="username"]').fill(server.username)
    await page.locator('input[name="password"]').fill(server.invalidPassword)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator('main section p[role="alert"]')).toContainText(/invalid|incorrect|无效|错误/i)

    if ((await page.locator('input[name="password"]').inputValue()).length !== 0) {
      throw new Error('Password field was not cleared after rejected login')
    }

    expect(server.events().some(event => event.name === 'login_rejected')).toBe(true)
    expect(keyringRecordExists(authVenvPython)).toBe(false)
    expect(backendDescendants(lockedRootPid!)).toHaveLength(0)

    for (const rejection of await protectedIpcRejections(page)) {
      expect(rejection).toContain('AUTH_REQUIRED')
    }

    expect(protectedRendererChunkRequested()).toBe(false)
    expectFullRuntimeAbsent(activeRoot)

    const acceptedBeforeInterruptedRuntime = server.events().filter(event => event.name === 'login_accepted').length
    await page.locator('input[name="username"]').fill(server.username)
    await page.locator('input[name="password"]').fill(server.password)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator('input[name="password"]')).toHaveCount(0, { timeout: 2 * 60_000 })
    await expectAuthenticated(page, server.username, false)
    await expect
      .poll(() => server!.events().filter(event => event.name === 'login_accepted').length)
      .toBeGreaterThan(acceptedBeforeInterruptedRuntime)
    await expect(page.getByRole('heading', { name: 'Account verified, preparing Hermes' })).toBeVisible()
    await expect
      .poll(
        async () => {
          const state = await safeBootstrapState(page!)

          return { scope: state.scope, status: state.status }
        },
        {
          timeout: 30_000,
          intervals: [250, 500, 1_000],
          message: 'runtime bootstrap did not enter the preparing state after login'
        }
      )
      .toEqual({ scope: 'runtime', status: 'preparing' })
    const interruptedRuntimeGeneration = (await safeBootstrapState(page)).generation
    expect(protectedRendererChunkRequested()).toBe(false)
    await expect(page.locator('[data-slot="statusbar"]')).toHaveCount(0)

    let interruptedRuntimeProcesses: WindowsProcessRow[] = []
    await expect
      .poll(
        () => {
          interruptedRuntimeProcesses = runtimeBootstrapDescendants(lockedRootPid!)

          return interruptedRuntimeProcesses.length
        },
        {
          timeout: 10 * 60_000,
          intervals: [250, 500, 1_000],
          message: 'full runtime bootstrap did not create a scoped Windows installer process'
        }
      )
      .toBeGreaterThan(0)

    const interruptedSnapshot = windowsProcessSnapshot()
    const interruptedRuntimePids = new Set<number>()

    for (const process of interruptedRuntimeProcesses) {
      interruptedRuntimePids.add(process.pid)

      for (const descendant of descendantsOf(interruptedSnapshot, process.pid)) {
        interruptedRuntimePids.add(descendant.pid)
      }
    }

    const elapsedLabel = page.getByText(/^Elapsed · /)
    await expect(elapsedLabel).toBeVisible()
    const elapsedBeforeBlur = elapsedSeconds((await elapsedLabel.textContent()) || '')
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.blur())
    await page.waitForTimeout(5_500)
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.focus())
    await expect
      .poll(async () => elapsedSeconds((await elapsedLabel.textContent()) || ''), {
        timeout: 10_000,
        intervals: [100, 250, 500]
      })
      .toBeGreaterThanOrEqual(elapsedBeforeBlur + 4)

    const logoutEventsBeforeQueuedRelogin = server.events().filter(event => event.name === 'logout').length
    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('heading', { name: 'Sign in to Hermes' })).toBeVisible()
    await expect(page.locator('[data-slot="statusbar"]')).toHaveCount(0)

    if (!fs.existsSync(fullLockPath)) {
      throw new Error('Installed bundled source did not contain the full runtime uv.lock')
    }

    savedFullLock = fs.readFileSync(fullLockPath)
    fs.writeFileSync(fullLockPath, 'version = 1\nrevision = 3\n', 'utf8')

    // Do not wait for the interrupted installer tree to disappear before
    // relogin. The coordinator must queue this login behind logout cleanup;
    // the runtime gate must then prevent the new generation from overlapping
    // the aborted one.
    const acceptedBeforeFailedRuntime = server.events().filter(event => event.name === 'login_accepted').length
    await page.locator('input[name="username"]').fill(server.username)
    await page.locator('input[name="password"]').fill(server.password)
    await page.locator('button[type="submit"]').click()
    await expectAuthenticated(page, server.username, false)
    await expect
      .poll(() => server!.events().filter(event => event.name === 'login_accepted').length)
      .toBeGreaterThan(acceptedBeforeFailedRuntime)
    await expect
      .poll(() => server!.events().filter(event => event.name === 'logout').length)
      .toBeGreaterThan(logoutEventsBeforeQueuedRelogin)

    const acceptedAt = server
      .events()
      .filter(event => event.name === 'login_accepted')
      .at(-1)!.at

    const logoutAt = server
      .events()
      .filter(event => event.name === 'logout')
      .at(-1)!.at

    expect(logoutAt).toBeLessThanOrEqual(acceptedAt)
    await expect
      .poll(
        async () => {
          const state = await safeBootstrapState(page!)

          return state.status === 'failed' && state.generation > interruptedRuntimeGeneration
        },
        {
          timeout: 10 * 60_000,
          intervals: [500, 1_000, 2_000]
        }
      )
      .toBe(true)
    const failedRuntimeState = await safeBootstrapState(page)
    await expect
      .poll(
        () => {
          const livePids = new Set(windowsProcessSnapshot().map(row => row.pid))

          return [...interruptedRuntimePids].filter(pid => livePids.has(pid))
        },
        {
          timeout: 30_000,
          intervals: [250, 500],
          message: 'interrupted runtime bootstrap process tree survived queued relogin'
        }
      )
      .toEqual([])
    await expect
      .poll(() => authBridgeRoots(authBridgeDescendants(lockedRootPid!)).length, {
        timeout: 30_000,
        intervals: [250, 500]
      })
      .toBe(1)
    await expect
      .poll(() => runtimeBootstrapDescendants(lockedRootPid!).map(row => row.pid), {
        timeout: 30_000,
        intervals: [250, 500]
      })
      .toEqual([])
    await expect
      .poll(() => backendDescendants(lockedRootPid!).map(row => row.pid), {
        timeout: 30_000,
        intervals: [250, 500]
      })
      .toEqual([])
    await expectAuthenticated(page, server.username, false)
    await expect(page.getByText('Hermes could not prepare its local runtime.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
    await expect(page.locator('body')).not.toContainText(/uv\.lock|locked authentication dependency|version = 1/i)
    await expect(page.locator('[data-slot="statusbar"]')).toHaveCount(0)
    expect(protectedRendererChunkRequested()).toBe(false)
    expect(backendDescendants(lockedRootPid!)).toHaveLength(0)
    expect(fs.existsSync(path.join(activeRoot, '.hermes-bootstrap-complete'))).toBe(false)
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    for (const rejection of await protectedIpcRejections(page)) {
      expect(rejection).not.toBe('ALLOWED')
      expect(rejection).toMatch(/AUTH_REQUIRED|RUNTIME_NOT_READY|runtime_unavailable/i)
    }

    fs.writeFileSync(fullLockPath, savedFullLock)
    await page.getByRole('button', { name: 'Retry' }).click()
    await expect
      .poll(
        async () => {
          const state = await safeBootstrapState(page!)

          return state.scope === 'runtime' && state.generation > failedRuntimeState.generation
        },
        { timeout: 30_000, intervals: [100, 250, 500] }
      )
      .toBe(true)
    await waitForInstalledFullRuntime(page, activeRoot, venvPython, 31 * 60_000)
    expect(installedRuntimeProtocols(venvPython, activeRoot)).toBe('2:2')
    await expectAuthenticated(page, server.username, true)
    await expectProtectedRendererChunk(protectedRendererChunkRequested)
    await expect(page.locator('[data-slot="statusbar"]')).toBeVisible({ timeout: 30_000 })
    assertBootstrapNodeDependenciesSucceeded(sandbox.hermesHome)
    assertInstalledPayloadBoundary(activeRoot)

    let firstBackendAt = 0
    let authenticatedBackend: WindowsProcessRow[] = []
    await expect
      .poll(
        () => {
          authenticatedBackend = backendDescendants(lockedRootPid!)

          if (authenticatedBackend.length > 0 && firstBackendAt === 0) {
            firstBackendAt = Date.now()
          }

          return authenticatedBackend.length
        },
        { timeout: 10 * 60_000, intervals: [500, 1_000, 2_000] }
      )
      .toBeGreaterThan(0)
    expect(firstBackendAt).toBeGreaterThan(acceptedAt)
    const venvPythonAliases = windowsPathAliases(venvPython)

    const claimedBackends = readBackendOwnership(sandbox.userDataDir).filter(entry => {
      const command = entry.command.toLowerCase()

      return venvPythonAliases.some(alias => command.includes(alias))
    })

    expect(claimedBackends, 'Desktop did not claim a backend launched through the isolated venv').toHaveLength(1)
    const processSnapshot = windowsProcessSnapshot()

    const claimedProcessIds = new Set([
      claimedBackends[0].pid,
      ...descendantsOf(processSnapshot, claimedBackends[0].pid).map(row => row.pid)
    ])

    expect(
      authenticatedBackend.filter(row => !claimedProcessIds.has(row.pid)),
      'authenticated backend was not descended from the isolated venv claim'
    ).toEqual([])
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    const activeBridgeProcesses = authBridgeDescendants(lockedRootPid!)
    const activeBridgeRoots = authBridgeRoots(activeBridgeProcesses)
    expect(activeBridgeRoots, 'installed Desktop did not own exactly one authentication bridge tree').toHaveLength(1)
    const failedBridgePids = activeBridgeProcesses.map(row => row.pid)
    const authOwnerPidsBeforeBridgeFailure = newAuthOwnerPids(initialOwnerPids).sort((left, right) => left - right)
    const sessionValidBeforeRecovery = server.events().filter(event => event.name === 'session_valid').length

    const stoppedBridgePids = await stopCapturedWindowsProcesses(activeBridgeProcesses)

    expect(stoppedBridgePids.every(pid => failedBridgePids.includes(pid))).toBe(true)
    expect(
      stoppedBridgePids.filter(pid => claimedProcessIds.has(pid)),
      'bridge fault injection targeted the backend'
    ).toEqual([])
    expect(newAuthOwnerPids(initialOwnerPids).sort((left, right) => left - right)).toEqual(
      authOwnerPidsBeforeBridgeFailure
    )
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('[data-slot="statusbar"]')).toHaveCount(0)
    await expect
      .poll(() => backendDescendants(lockedRootPid!).map(row => row.pid), {
        timeout: 30_000,
        intervals: [250, 500],
        message: 'backend survived authentication bridge failure for 30 seconds'
      })
      .toEqual([])
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    await page.getByRole('button', { name: 'Retry' }).click()

    await expectAuthenticated(page, server.username, true)
    await expect(page.locator('input[name="password"]')).toHaveCount(0)
    await expect(page.locator('[data-slot="statusbar"]')).toBeVisible({ timeout: 30_000 })
    await expect
      .poll(() => server!.events().filter(event => event.name === 'session_valid').length, { timeout: 30_000 })
      .toBeGreaterThan(sessionValidBeforeRecovery)
    await expect
      .poll(
        () => {
          const bridgeProcesses = authBridgeDescendants(lockedRootPid!)

          return {
            oldPids: bridgeProcesses.map(row => row.pid).filter(pid => failedBridgePids.includes(pid)),
            roots: authBridgeRoots(bridgeProcesses).length
          }
        },
        {
          timeout: 30_000,
          intervals: [250, 500]
        }
      )
      .toEqual({ oldPids: [], roots: 1 })
    await expect
      .poll(() => backendDescendants(lockedRootPid!).length, {
        timeout: 10 * 60_000,
        intervals: [500, 1_000, 2_000]
      })
      .toBeGreaterThan(0)
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    const sessionValidBeforeRestart = server.events().filter(event => event.name === 'session_valid').length
    server.setMode('500')
    await closeDesktopApp(app, { timeoutMs: 10_000 })
    app = null
    page = null
    await stopNewAuthOwnerProcesses(initialOwnerPids)

    launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    await expectAuthenticated(page, server.username, true)
    await expect(page.locator('input[name="username"]')).toHaveCount(0)
    await expect(page.locator('[data-slot="statusbar"]')).toBeVisible({ timeout: 30_000 })
    const restoredRootPid = app.process().pid
    expect(restoredRootPid).toBeDefined()
    await expect
      .poll(() => backendDescendants(restoredRootPid!).length, {
        timeout: 10 * 60_000,
        intervals: [500, 1_000, 2_000]
      })
      .toBeGreaterThan(0)
    expect(keyringRecordExists(authVenvPython)).toBe(true)

    const restoredBackendPids = backendDescendants(restoredRootPid!)
      .map(row => row.pid)
      .sort((left, right) => left - right)

    await expect
      .poll(
        async () => {
          const status = await installedAccountStatus(page!)

          return {
            state: status.state,
            cloud_state: status.cloud_state,
            validation_state: status.validation_state,
            validation_reason: status.validation_reason,
            runtime_ready: status.runtime_ready
          }
        },
        {
          timeout: 2 * 60_000,
          intervals: [500, 1_000, 2_000],
          message: 'installed cached authorization did not degrade safely during an auth 5xx'
        }
      )
      .toEqual({
        state: 'authenticated',
        cloud_state: 'unreachable',
        validation_state: 'degraded',
        validation_reason: 'server_unavailable',
        runtime_ready: true
      })
    expect(
      backendDescendants(restoredRootPid!)
        .map(row => row.pid)
        .sort((left, right) => left - right)
    ).toEqual(restoredBackendPids)
    await expect(page.locator('[data-slot="statusbar"]')).toBeVisible()
    await expect(page.locator('input[name="username"]')).toHaveCount(0)

    server.setMode('online')
    await expect
      .poll(
        async () => {
          const status = await installedAccountStatus(page!)

          return { cloud_state: status.cloud_state, validation_state: status.validation_state }
        },
        {
          timeout: 2 * 60_000,
          intervals: [500, 1_000, 2_000],
          message: 'installed cached authorization did not silently revalidate after auth recovery'
        }
      )
      .toEqual({ cloud_state: 'active', validation_state: 'online' })
    await expect
      .poll(() => server!.events().filter(event => event.name === 'session_valid').length, { timeout: 30_000 })
      .toBeGreaterThan(sessionValidBeforeRestart)
    expect(
      backendDescendants(restoredRootPid!)
        .map(row => row.pid)
        .sort((left, right) => left - right)
    ).toEqual(restoredBackendPids)

    const logoutStatus = await page.evaluate(() =>
      (
        window as unknown as {
          hermesDesktop: { auth: { logout: () => Promise<{ state: string }> } }
        }
      ).hermesDesktop.auth.logout()
    )

    expect(logoutStatus.state).toBe('signed_out')
    await expect(page.locator('input[name="username"]')).toBeVisible()
    await expect(page.locator('textarea, [contenteditable="true"]')).toHaveCount(0)
    await expect(page.locator('[data-slot="statusbar"]')).toHaveCount(0)
    await expect
      .poll(() => backendDescendants(restoredRootPid!).map(row => row.pid), {
        timeout: 30_000,
        intervals: [250, 500],
        message: 'backend survived logout for 30 seconds'
      })
      .toEqual([])

    for (const rejection of await protectedIpcRejections(page)) {
      expect(rejection).not.toBe('ALLOWED')
      expect(rejection).toContain('AUTH_REQUIRED')
    }

    expect(keyringRecordExists(authVenvPython)).toBe(false)
    expect(server.events().some(event => event.name === 'logout')).toBe(true)

    await closeDesktopApp(app, { timeoutMs: 10_000 })
    app = null
    page = null
    await stopNewAuthOwnerProcesses(initialOwnerPids)
    launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    page = launched.page
    await expect(page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    expect((await installedAccountStatus(page)).state).toBe('signed_out')
    const signedOutRootPid = app.process().pid
    expect(signedOutRootPid).toBeDefined()
    expect(backendDescendants(signedOutRootPid!)).toHaveLength(0)
    expect(keyringRecordExists(authVenvPython)).toBe(false)

    for (const [index, value] of server.sensitiveValues().entries()) {
      generatedSensitiveValues.push({ category: `auth-contract-${index}`, value })
    }

    const scanFiles: string[] = []
    const scanStack = [sandbox.root, path.join(REPO_ROOT, 'apps', 'desktop', 'build', 'logs')]
    const textExtensions = new Set(['.log', '.json', '.txt', '.yaml', '.yml'])

    while (scanStack.length > 0) {
      const candidate = scanStack.pop()!

      if (!fs.existsSync(candidate)) {
        continue
      }

      const stats = fs.lstatSync(candidate)

      if (stats.isSymbolicLink()) {
        continue
      }

      if (stats.isDirectory()) {
        for (const entry of fs.readdirSync(candidate)) {
          scanStack.push(path.join(candidate, entry))
        }
      } else if (
        textExtensions.has(path.extname(candidate).toLowerCase()) &&
        (candidate.startsWith(sandbox.root) || /^phase1-desktop-windows-nsis-.*\.log$/i.test(path.basename(candidate)))
      ) {
        scanFiles.push(candidate)
      }
    }

    for (const filePath of scanFiles) {
      const contents = fs.readFileSync(filePath, 'utf8')

      for (const sensitive of generatedSensitiveValues) {
        if (contents.includes(sensitive.value)) {
          throw new Error(`Sensitive value category ${sensitive.category} leaked into ${filePath}`)
        }
      }
    }
  } catch (error) {
    preserveBootstrapDiagnostics(sandbox.hermesHome)
    throw error
  } finally {
    if (savedFullLock && fs.existsSync(path.dirname(fullLockPath))) {
      fs.writeFileSync(fullLockPath, savedFullLock)
    }

    try {
      if (app) {
        await closeDesktopApp(app, { timeoutMs: 10_000 })
      }
    } finally {
      try {
        await stopNewAuthOwnerProcesses(initialOwnerPids)
      } finally {
        try {
          if (wrongSanServer) {
            await wrongSanServer.close()
          }

          if (server) {
            await server.close()
          }
        } finally {
          try {
            const cleanupPython = fs.existsSync(authVenvPython) ? authVenvPython : repoPython
            execFileSync(cleanupPython, ['-'], {
              encoding: 'utf8',
              input: `import keyring
from keyring.errors import PasswordDeleteError
try:
    keyring.delete_password(${JSON.stringify(KEYRING_SERVICE)}, ${JSON.stringify(KEYRING_ACCOUNT)})
except PasswordDeleteError:
    pass
`
            })
            expect(createHash('sha256').update(fs.readFileSync(repoCertifiBundle)).digest('hex')).toBe(
              repoCertifiHashBefore
            )
          } finally {
            sandbox.cleanup()
            expect(fs.existsSync(sandbox.root)).toBe(false)
          }
        }
      }
    }
  }
})
