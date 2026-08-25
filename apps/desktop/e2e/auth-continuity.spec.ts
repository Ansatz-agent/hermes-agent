import { execFileSync, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { ElectronApplication, Page } from '@playwright/test'

import {
  assertSensitiveValuesAbsent,
  authStatus,
  type AuthStatus,
  backendProcessIds,
  localDataDigests,
  protectedIpcRejections
} from './auth-assertions'
import { type FixedAuthContractServer, startFixedAuthContractServer } from './fixed-auth-contract-server'
import {
  buildAppEnv,
  closeDesktopApp,
  createSandbox,
  launchDesktop,
  type Sandbox,
  writeEnvFile,
  writeMockProviderConfig
} from './fixtures'
import { MOCK_REPLY, type MockServer, startMockServer } from './mock-server'
import { allowErrorBanners, expect, test } from './test'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')
const PYTHON = process.env.HERMES_PYTHON || '/Users/yuxiaoy/miniconda3/envs/dl/bin/python'
const SENTINEL = 'offline continuity sentinel'
const AUTH_OWNER_COMMAND = /(?:^|\s)-m\s+hermes_cli\.client_auth\.runtime\s+owner(?:\s|$)/

type ControllableAuthServer = FixedAuthContractServer & {
  setMode(mode: 'online' | 'timeout' | '429' | '500' | 'malformed'): void
}

interface AuthenticatedFixture {
  app: ElectronApplication
  page: Page
  sandbox: Sandbox
  server: ControllableAuthServer
  mock: MockServer
  credentialStorePath: string
  environment: Record<string, string>
  runtimeRoot: string
  restart(): Promise<void>
  cleanup(): Promise<void>
}

test.skip(
  process.platform === 'win32',
  'The installed Windows continuity proof lives in installed-windows-auth.spec.ts'
)
test.describe.configure({ mode: 'serial' })
test.setTimeout(300_000)

test('cached authorization restarts offline and silently revalidates', async () => {
  allowErrorBanners()
  const fixture = await launchAuthenticatedFixture()

  try {
    await login(fixture)
    await createConversation(fixture.page, SENTINEL)
    createLocalArtifacts(fixture)
    const before = localDigests(fixture)

    fixture.server.setMode('timeout')
    await fixture.restart()

    await expect(fixture.page.getByText(SENTINEL).first()).toBeVisible({ timeout: 60_000 })
    await expect(fixture.page.locator('[contenteditable="true"]').first()).toBeVisible({ timeout: 60_000 })
    await expect(fixture.page.getByRole('heading', { name: /sign in to (?:ansatz|hermes)/i })).toHaveCount(0)
    expect(backendProcessIds(fixture.app)).not.toEqual([])

    fixture.server.setMode('online')
    await expect
      .poll(() => validationState(fixture.page), {
        timeout: 60_000,
        intervals: [100, 250, 500, 1_000]
      })
      .toBe('online')
    expect(localDigests(fixture)).toEqual(before)

    for (const [mode, reason] of [
      ['timeout', null],
      ['429', 'rate_limited'],
      ['500', 'server_unavailable'],
      ['malformed', 'invalid_response']
    ] as const) {
      const backendBefore = backendProcessIds(fixture.app)
      const marker = `continuity-${mode}-${randomBytes(6).toString('hex')}`
      await fixture.page
        .locator('[contenteditable="true"]')
        .first()
        .evaluate((element, value) => element.setAttribute('data-auth-continuity-marker', value), marker)

      fixture.server.setMode(mode)
      if (mode === 'timeout') {
        await expect.poll(() => fixture.server.heldRequestCount(), { timeout: 5_000 }).toBeGreaterThan(0)
      } else {
        await expect
          .poll(() => authStatus(fixture.page), { timeout: 10_000, intervals: [100, 250, 500] })
          .toMatchObject({ state: 'authenticated', validation_state: 'degraded', validation_reason: reason })
      }

      expect(backendProcessIds(fixture.app)).toEqual(backendBefore)
      await expect(fixture.page.locator(`[data-auth-continuity-marker="${marker}"]`)).toHaveCount(1)
      await expect(fixture.page.getByText(SENTINEL).first()).toBeVisible()
      await expect(fixture.page.getByRole('heading', { name: /sign in to (?:ansatz|hermes)/i })).toHaveCount(0)

      fixture.server.setMode('online')
      await expect
        .poll(() => validationState(fixture.page), { timeout: 10_000, intervals: [100, 250, 500] })
        .toBe('online')
      expect(backendProcessIds(fixture.app)).toEqual(backendBefore)
    }

    const backendBeforeOwnerRestart = backendProcessIds(fixture.app)
    await restartAuthOwner(fixture.runtimeRoot)
    await expect
      .poll(() => authStatus(fixture.page), { timeout: 30_000, intervals: [100, 250, 500] })
      .toMatchObject({ state: 'authenticated', runtime_ready: true, validation_state: 'online' })
    await expect
      .poll(() => backendProcessIds(fixture.app), { timeout: 10_000, intervals: [100, 250, 500] })
      .toEqual(backendBeforeOwnerRestart)
    await expect(fixture.page.getByText(SENTINEL).first()).toBeVisible()

    assertNoCredentialDiagnostics(fixture)
  } finally {
    await fixture.cleanup()
  }
})

test('sign out clears access but preserves every local user artifact', async () => {
  allowErrorBanners()
  const fixture = await launchAuthenticatedFixture()

  try {
    await login(fixture)
    createLocalArtifacts(fixture)
    const before = localDigests(fixture)
    assertRequiredDigestCoverage(before)
    expect(fs.existsSync(fixture.credentialStorePath)).toBe(true)
    expect(backendProcessIds(fixture.app)).not.toEqual([])

    const logoutStatus = await fixture.page.evaluate(() =>
      (
        window as unknown as { hermesDesktop: { auth: { logout: () => Promise<AuthStatus> } } }
      ).hermesDesktop.auth.logout()
    )

    expect(logoutStatus).toMatchObject({ state: 'signed_out' })
    await expect(fixture.page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
    await expect.poll(() => authStatus(fixture.page)).toMatchObject({ state: 'signed_out' })
    await expect.poll(() => backendProcessIds(fixture.app), { timeout: 30_000, intervals: [100, 250, 500] }).toEqual([])
    expect(fs.existsSync(fixture.credentialStorePath)).toBe(false)
    for (const rejection of await protectedIpcRejections(fixture.page)) {
      expect(rejection).not.toBe('ALLOWED')
      expect(rejection).toContain('AUTH_REQUIRED')
    }
    expect(localDigests(fixture)).toEqual(before)
    expect(JSON.stringify(before)).not.toMatch(/credential|keyring/i)
    assertNoCredentialDiagnostics(fixture)
  } finally {
    await fixture.cleanup()
  }
})

for (const reason of ['account_disabled', 'account_revoked', 'session_revoked'] as const) {
  test(`matching ${reason} cleans up exactly once without deleting local data`, async () => {
    allowErrorBanners()
    const fixture = await launchAuthenticatedFixture()

    try {
      await login(fixture)
      createLocalArtifacts(fixture)
      const before = localDigests(fixture)
      const expectedIdentity = fixture.server.currentIdentity()
      await fixture.page.evaluate(expectedReason => {
        const state = window as typeof window & { __terminalCleanupCount?: number; __terminalCleanupStop?: () => void }
        const auth = (
          window as unknown as {
            hermesDesktop: { auth: { onChanged: (callback: (status: AuthStatus) => void) => () => void } }
          }
        ).hermesDesktop.auth
        state.__terminalCleanupCount = 0
        state.__terminalCleanupStop?.()
        state.__terminalCleanupStop = auth.onChanged(status => {
          if (status.reason === expectedReason) {
            state.__terminalCleanupCount = (state.__terminalCleanupCount ?? 0) + 1
          }
        })
      }, reason)

      const revocationResponsesBefore = fixture.server
        .events()
        .filter(event => event.name === 'revocation_response').length
      fixture.server.revokeCurrent(reason)
      await expect
        .poll(() => authStatus(fixture.page), { timeout: 10_000 })
        .toMatchObject({
          state: 'locked',
          reason,
          account_id: expectedIdentity.accountId,
          session_id: expectedIdentity.sessionId
        })
      expect(fixture.server.events().filter(event => event.name === 'revocation_response').length).toBeGreaterThan(
        revocationResponsesBefore
      )
      await expect(fixture.page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
      await expect
        .poll(() => backendProcessIds(fixture.app), { timeout: 30_000, intervals: [100, 250, 500] })
        .toEqual([])
      await expect
        .poll(
          () =>
            fixture.page.evaluate(
              () => (window as typeof window & { __terminalCleanupCount?: number }).__terminalCleanupCount ?? 0
            ),
          { timeout: 10_000 }
        )
        .toBe(1)
      expect(localDigests(fixture)).toEqual(before)
      assertNoCredentialDiagnostics(fixture)
    } finally {
      await fixture.cleanup()
    }
  })
}

async function launchAuthenticatedFixture(): Promise<AuthenticatedFixture> {
  if (!fs.existsSync(PYTHON)) {
    throw new Error(`Hermes E2E Python is unavailable: ${PYTHON}`)
  }

  const sandbox = createSandbox('auth-continuity')
  const certPath = path.join(sandbox.root, 'auth-cert.pem')
  const keyPath = path.join(sandbox.root, 'auth-key.pem')
  execFileSync(
    'openssl',
    [
      'req',
      '-x509',
      '-newkey',
      'rsa:2048',
      '-nodes',
      '-days',
      '1',
      '-subj',
      '/CN=c2sml.cn',
      '-addext',
      'subjectAltName=DNS:c2sml.cn',
      '-keyout',
      keyPath,
      '-out',
      certPath
    ],
    { stdio: 'ignore' }
  )

  const server = (await startFixedAuthContractServer({
    certPath,
    keyPath,
    listenPort: 0
  })) as ControllableAuthServer
  const mock = await startMockServer()
  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)

  const credentialStorePath = path.join(sandbox.root, 'auth-credential-store.json')
  const isolation = `auth-continuity-${randomBytes(16).toString('hex')}`
  const runtimeRoot = createInstrumentedRuntimeRoot({
    sandbox,
    serverPort: server.listenPort,
    certPath,
    credentialStorePath,
    isolation
  })
  const environment = buildAppEnv(sandbox, {
    HERMES_DESKTOP_CWD: sandbox.root,
    HERMES_DESKTOP_HERMES_ROOT: runtimeRoot,
    HERMES_AUTH_RUNTIME_NAMESPACE: isolation,
    HERMES_AUTH_KEYRING_SERVICE: `cn.c2sml.test.${randomBytes(8).toString('hex')}`
  })
  let launched = await launchDesktop(environment)

  const fixture: AuthenticatedFixture = {
    app: launched.app,
    page: launched.page,
    sandbox,
    server,
    mock,
    credentialStorePath,
    environment,
    runtimeRoot,
    restart: async () => {
      await closeDesktopApp(fixture.app, { timeoutMs: 15_000 })
      launched = await launchDesktop(environment)
      fixture.app = launched.app
      fixture.page = launched.page
    },
    cleanup: async () => {
      try {
        await closeDesktopApp(fixture.app, { timeoutMs: 15_000 })
      } catch {
        // A failed assertion may have already closed the app.
      }
      await stopNewAuthOwners(runtimeRoot)
      await mock.close()
      await server.close()
      sandbox.cleanup()
    }
  }

  return fixture
}

function createInstrumentedRuntimeRoot(options: {
  sandbox: Sandbox
  serverPort: number
  certPath: string
  credentialStorePath: string
  isolation: string
}): string {
  const runtimeRoot = path.join(options.sandbox.hermesHome, 'hermes-agent')
  fs.mkdirSync(runtimeRoot, { recursive: true })

  for (const entry of fs.readdirSync(REPO_ROOT)) {
    if (entry === 'hermes_cli' || entry === 'agent') {
      fs.cpSync(path.join(REPO_ROOT, entry), path.join(runtimeRoot, entry), { recursive: true })
    } else {
      fs.symlinkSync(path.join(REPO_ROOT, entry), path.join(runtimeRoot, entry))
    }
  }

  const venvBin = path.join(runtimeRoot, 'venv', 'bin')
  fs.mkdirSync(venvBin, { recursive: true })
  fs.symlinkSync(PYTHON, path.join(venvBin, 'python'))
  fs.writeFileSync(
    path.join(runtimeRoot, '.hermes-bootstrap-complete'),
    JSON.stringify(
      {
        schemaVersion: 1,
        pinnedCommit: 'f8d8f47fbc9d310d1a60d4cd357dd727293c54e4',
        pinnedBranch: 'feature/auth-continuity',
        completedAt: new Date().toISOString(),
        desktopVersion: 'e2e'
      },
      null,
      2
    ) + '\n',
    'utf8'
  )

  const clientPath = path.join(runtimeRoot, 'hermes_cli', 'client_auth', 'client.py')
  let clientSource = fs.readFileSync(clientPath, 'utf8')
  clientSource = replaceOnce(
    clientSource,
    'AUTH_ORIGIN = "https://c2sml.cn"',
    `AUTH_ORIGIN = "https://c2sml.cn:${options.serverPort}"`
  )
  clientSource = replaceOnce(
    clientSource,
    'AUTH_FALLBACK_ADDRESS = "121.37.182.49"',
    'AUTH_FALLBACK_ADDRESS = "127.0.0.1"'
  )
  clientSource = replaceOnce(
    clientSource,
    'httpx.create_ssl_context(trust_env=False)',
    `httpx.create_ssl_context(verify=${JSON.stringify(options.certPath)}, trust_env=False)`
  )
  clientSource = replaceOnce(clientSource, 'transport=transport,', 'transport=transport or _PinnedAuthTransport(),')
  fs.writeFileSync(clientPath, clientSource, 'utf8')

  const tracePolicyPath = path.join(runtimeRoot, 'agent', 'ansatz_trace_policy.py')
  let tracePolicySource = fs.readFileSync(tracePolicyPath, 'utf8')
  tracePolicySource = replaceOnce(
    tracePolicySource,
    `def ansatz_product_trace_requested() -> bool:
    """Return whether a Desktop/Voice child declared any product trace state."""
    return any(`,
    `def ansatz_product_trace_requested() -> bool:
    """Disable product Trace activation only inside the auth-continuity sandbox."""
    return False

def _unused_original_ansatz_product_trace_requested() -> bool:
    return any(`
  )
  fs.writeFileSync(tracePolicyPath, tracePolicySource, 'utf8')

  const runtimePath = path.join(runtimeRoot, 'hermes_cli', 'client_auth', 'runtime.py')
  let runtimeSource = fs.readFileSync(runtimePath, 'utf8')
  runtimeSource = replaceOnce(
    runtimeSource,
    `        delay = self._jitter(57.0, 60.0)
        if not 57.0 <= delay <= 60.0:
            self._publish_locked(self._snapshot.locked("runtime_unavailable", now=now))
            self._next_refresh_at = None
            raise AuthRequired("runtime_unavailable")
        self._next_refresh_at = min(now + delay, self._snapshot.valid_until)`,
    '        self._next_refresh_at = min(now + 0.5, self._snapshot.valid_until)'
  )
  runtimeSource = replaceOnce(
    runtimeSource,
    '\nif __name__ == "__main__":\n',
    `
def _auth_runtime_namespace():
    return ${JSON.stringify(options.isolation)}

class _TestFileSecretBackend:
    ACCOUNT = "django-session"
    SERVICE = "cn.c2sml.test.auth-continuity"
    def __init__(self, **_kwargs):
        self._path = Path(${JSON.stringify(options.credentialStorePath)})
    def read(self):
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
    def write(self, raw):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        pending = self._path.with_suffix(".pending")
        pending.write_text(raw, encoding="utf-8")
        os.chmod(pending, 0o600)
        pending.replace(self._path)
    def delete(self):
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

_KeyringSecretBackend = _TestFileSecretBackend

if __name__ == "__main__":
`
  )
  fs.writeFileSync(runtimePath, runtimeSource, 'utf8')

  return runtimeRoot
}

function replaceOnce(source: string, target: string, replacement: string): string {
  const first = source.indexOf(target)
  if (first < 0 || source.indexOf(target, first + target.length) >= 0) {
    throw new Error(`Expected exactly one sandbox instrumentation target: ${target}`)
  }
  return source.slice(0, first) + replacement + source.slice(first + target.length)
}

async function login(fixture: AuthenticatedFixture): Promise<void> {
  try {
    await expect(fixture.page.locator('input[name="username"]')).toBeVisible({ timeout: 30_000 })
  } catch (error) {
    const state = await fixture.page.evaluate(() =>
      (
        window as unknown as { hermesDesktop: { authBootstrap: { getState: () => Promise<unknown> } } }
      ).hermesDesktop.authBootstrap.getState()
    )
    const logRoot = path.join(fixture.sandbox.hermesHome, 'logs')
    const logs = fs.existsSync(logRoot)
      ? fs
          .readdirSync(logRoot)
          .filter(name => /^bootstrap-.*\.log$/i.test(name))
          .map(name => fs.readFileSync(path.join(logRoot, name), 'utf8'))
          .join('\n')
          .slice(-8_000)
      : ''
    throw new Error(`Login form unavailable: ${String(error)}\nbootstrap=${JSON.stringify(state)}\n${logs}`)
  }
  await fixture.page.locator('input[name="username"]').fill(fixture.server.username)
  await fixture.page.locator('input[name="password"]').fill(fixture.server.password)
  await fixture.page.locator('button[type="submit"]').click()
  try {
    await expect(fixture.page.locator('[contenteditable="true"]').first()).toBeVisible({ timeout: 120_000 })
  } catch (error) {
    const status = await authStatus(fixture.page)
    throw new Error(
      `Protected client unavailable after login: ${String(error)}\nstatus=${JSON.stringify(status)}\nevents=${JSON.stringify(fixture.server.events())}\ndiagnostics=${JSON.stringify(fixture.server.diagnostics())}`
    )
  }
}

async function createConversation(page: Page, text: string): Promise<void> {
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.click()
  await composer.type(text, { delay: 5 })
  await page.keyboard.press('Enter')
  await expect(page.getByText(text).first()).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(MOCK_REPLY).first()).toBeVisible({ timeout: 60_000 })
}

async function validationState(page: Page): Promise<string> {
  return authStatus(page).then(status => status.validation_state)
}

function createLocalArtifacts(fixture: AuthenticatedFixture): void {
  const attachmentPath = path.join(
    fixture.sandbox.hermesHome,
    'kanban',
    'attachments',
    'auth-continuity-task',
    'attachment-preservation.bin'
  )
  const exportPath = path.join(fixture.sandbox.hermesHome, 'exports', 'conversation.json')
  const outboxPath = path.join(
    fixture.sandbox.userDataDir,
    'trace-outbox',
    fixture.server.currentIdentity().accountId,
    'segments',
    '0000000000000001.jsonl'
  )
  for (const filePath of [attachmentPath, exportPath, outboxPath]) {
    fs.mkdirSync(path.dirname(filePath), { recursive: true })
  }
  fs.writeFileSync(attachmentPath, Buffer.from([0, 1, 2, 3, 255]))
  fs.writeFileSync(exportPath, JSON.stringify({ conversation: 'preserve on auth transition' }) + '\n', 'utf8')
  fs.writeFileSync(outboxPath, JSON.stringify({ batch_id: 'auth-continuity-batch', trace: 'preserve' }) + '\n', 'utf8')

  const rootStateDatabase = path.join(fixture.sandbox.hermesHome, 'state.db')
  const profileStateDatabase = path.join(fixture.sandbox.hermesHome, 'profiles', 'research', 'state.db')
  fs.mkdirSync(path.dirname(profileStateDatabase), { recursive: true })
  const profileSeed = spawnSync(
    PYTHON,
    [
      '-c',
      'import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()',
      rootStateDatabase,
      profileStateDatabase
    ],
    { encoding: 'utf8' }
  )
  if (profileSeed.status !== 0) {
    throw new Error(`Unable to seed the profile SessionDB checkpoint: ${profileSeed.stderr.trim()}`)
  }

  const databases = [rootStateDatabase, path.join(fixture.sandbox.hermesHome, 'projects.db'), profileStateDatabase]
  for (const databasePath of databases) {
    fs.mkdirSync(path.dirname(databasePath), { recursive: true })
    const result = spawnSync(
      PYTHON,
      [
        '-c',
        'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE IF NOT EXISTS auth_continuity_e2e (value TEXT PRIMARY KEY)"); c.execute("INSERT OR IGNORE INTO auth_continuity_e2e VALUES (?)", ("preserve",)); c.commit(); c.close()',
        databasePath
      ],
      { encoding: 'utf8' }
    )
    if (result.status !== 0) {
      throw new Error(`Unable to create SQLite preservation artifact ${databasePath}: ${result.stderr.trim()}`)
    }
  }
}

function localDigests(fixture: AuthenticatedFixture): Record<string, string> {
  return localDataDigests({
    hermesHome: fixture.sandbox.hermesHome,
    userDataDir: fixture.sandbox.userDataDir,
    python: PYTHON
  })
}

function assertRequiredDigestCoverage(digests: Record<string, string>): void {
  const keys = Object.keys(digests)
  for (const expected of [
    /hermes\/state\.db#logical$/,
    /hermes\/projects\.db#logical$/,
    /hermes\/profiles\/research\/state\.db#logical$/,
    /hermes\/kanban\/attachments\/.*attachment-preservation\.bin$/,
    /hermes\/exports\/conversation\.json$/,
    /desktop\/trace-outbox\/.*0000000000000001\.jsonl$/
  ]) {
    expect(
      keys.some(key => expected.test(key)),
      `Digest report did not cover ${expected}`
    ).toBe(true)
  }
}

function assertNoCredentialDiagnostics(fixture: AuthenticatedFixture): void {
  const sensitive = fixture.server.sensitiveValues()
  for (const diagnostic of fixture.server.diagnostics()) {
    for (const value of sensitive) {
      expect(diagnostic).not.toContain(value)
    }
  }
  assertSensitiveValuesAbsent(
    [path.join(fixture.sandbox.hermesHome, 'logs'), path.join(fixture.sandbox.userDataDir, 'logs')],
    sensitive
  )
}

async function restartAuthOwner(runtimeRoot: string): Promise<void> {
  const owners = authOwnerRows().filter(row => row.command.includes(runtimeRoot))
  if (owners.length !== 1) {
    throw new Error(`Expected exactly one sandbox auth owner, found ${owners.length}`)
  }
  terminateExactOwner(owners[0], 'TERM')
  await expect
    .poll(() => authOwnerRows().some(row => row.pid === owners[0]!.pid), {
      timeout: 10_000,
      intervals: [50, 100, 250]
    })
    .toBe(false)
  await expect
    .poll(() => authOwnerRows().filter(row => row.command.includes(runtimeRoot) && row.pid !== owners[0]!.pid).length, {
      timeout: 10_000,
      intervals: [50, 100, 250]
    })
    .toBe(1)
}

async function stopNewAuthOwners(runtimeRoot: string): Promise<void> {
  const candidates = authOwnerRows().filter(row => row.command.includes(runtimeRoot))
  for (const row of candidates) {
    terminateExactOwner(row, 'TERM')
  }
  const deadline = Date.now() + 5_000
  while (
    Date.now() < deadline &&
    candidates.some(candidate => authOwnerRows().some(row => row.pid === candidate.pid))
  ) {
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  const survivors = candidates.filter(candidate => authOwnerRows().some(row => row.pid === candidate.pid))
  for (const survivor of survivors) {
    const current = authOwnerRows().find(row => row.pid === survivor.pid)
    if (current && current.command === survivor.command) {
      terminateExactOwner(current, 'KILL')
    }
  }
  if (survivors.length > 0) {
    throw new Error(`Auth owner teardown required SIGKILL for PIDs: ${survivors.map(row => row.pid).join(', ')}`)
  }
}

function terminateExactOwner(row: { command: string; pid: number }, signal: 'KILL' | 'TERM'): void {
  const current = authOwnerRows().find(candidate => candidate.pid === row.pid)
  if (!current || current.command !== row.command || !AUTH_OWNER_COMMAND.test(current.command)) {
    throw new Error(`Refusing to terminate changed auth owner PID ${row.pid}`)
  }
  const result = spawnSync('rtk', ['proxy', 'kill', `-${signal}`, String(row.pid)], { encoding: 'utf8' })
  if (result.status !== 0 && authOwnerRows().some(candidate => candidate.pid === row.pid)) {
    throw new Error(`Unable to terminate auth owner PID ${row.pid}: ${result.stderr.trim()}`)
  }
}

function authOwnerRows(): Array<{ command: string; pid: number }> {
  const result = spawnSync('ps', ['-axo', 'pid=,command='], { encoding: 'utf8' })
  if (result.status !== 0) {
    throw new Error('Unable to inspect auth owner processes')
  }
  return result.stdout
    .split('\n')
    .map(line => line.trim().match(/^(\d+)\s+(.+)$/))
    .filter((match): match is RegExpMatchArray => Boolean(match))
    .filter(match => AUTH_OWNER_COMMAND.test(match[2]!))
    .map(match => ({ pid: Number(match[1]), command: match[2]! }))
}
