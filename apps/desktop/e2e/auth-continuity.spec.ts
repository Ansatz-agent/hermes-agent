import { execFileSync, spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import net from 'node:net'
import path from 'node:path'

import type { ElectronApplication, Page } from '@playwright/test'

import {
  assertLocalDataPreserved,
  assertSensitiveValuesAbsent,
  authStatus,
  type AuthStatus,
  backendProcessIds,
  localDataDigests,
  type LocalDataEvidence,
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
  await test.step('fixture construction failure rolls back every acquired resource', async () => {
    await assertFixtureConstructionRollback()
    await assertCleanupContinuesAfterFailure()
  })
  const fixture = await launchAuthenticatedFixture()

  try {
    await login(fixture)
    await createConversation(fixture.page, SENTINEL)
    await createLocalArtifacts(fixture, SENTINEL)
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
    assertLocalDataPreserved(before, localDigests(fixture))

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
    const conversation = 'signout preservation conversation'
    await createConversation(fixture.page, conversation)
    await createLocalArtifacts(fixture, conversation)
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
    assertLocalDataPreserved(before, localDigests(fixture))
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
      const conversation = `${reason} preservation conversation`
      await createConversation(fixture.page, conversation)
      await createLocalArtifacts(fixture, conversation)
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
      assertLocalDataPreserved(before, localDigests(fixture))
      assertNoCredentialDiagnostics(fixture)
    } finally {
      await fixture.cleanup()
    }
  })
}

interface FixtureFailureSnapshot {
  appPid: number
  mockPort: number
  root: string
  runtimeRoot: string
  serverPort: number
}

async function assertFixtureConstructionRollback(): Promise<void> {
  let observed: FixtureFailureSnapshot | undefined
  let returned: AuthenticatedFixture | undefined
  let thrown: unknown
  const launchWithFault = launchAuthenticatedFixture as unknown as (options: {
    afterLaunch(snapshot: FixtureFailureSnapshot): never
  }) => Promise<AuthenticatedFixture>

  try {
    returned = await launchWithFault({
      afterLaunch: snapshot => {
        observed = snapshot
        throw new Error('fixture-construction-fault')
      }
    })
  } catch (error) {
    thrown = error
  } finally {
    await returned?.cleanup()
  }

  expect(String(thrown)).toContain('fixture-construction-fault')
  expect(observed).toBeDefined()
  expect(fs.existsSync(observed!.root)).toBe(false)
  expect(authOwnerRows().some(row => row.command.includes(observed!.runtimeRoot))).toBe(false)
  expect(processExists(observed!.appPid)).toBe(false)
  await expect.poll(() => tcpPortClosed(observed!.serverPort), { timeout: 5_000 }).toBe(true)
  await expect.poll(() => tcpPortClosed(observed!.mockPort), { timeout: 5_000 }).toBe(true)
}

async function assertCleanupContinuesAfterFailure(): Promise<void> {
  const calls: string[] = []
  let thrown: unknown

  try {
    await cleanupAuthenticatedResources({
      mock: {
        close: async () => {
          calls.push('mock')
          throw new Error('injected mock cleanup failure')
        }
      } as unknown as MockServer,
      sandbox: {
        cleanup: () => calls.push('sandbox')
      } as unknown as Sandbox,
      server: {
        close: async () => {
          calls.push('auth')
        }
      } as unknown as ControllableAuthServer
    })
  } catch (error) {
    thrown = error
  }

  expect(thrown).toBeInstanceOf(AggregateError)
  expect(String(thrown)).toContain('Authenticated fixture cleanup failed')
  expect(calls.sort()).toEqual(['auth', 'mock', 'sandbox'])
}

function processExists(pid: number): boolean {
  const result = spawnSync('ps', ['-p', String(pid), '-o', 'pid='], { encoding: 'utf8' })

  return result.status === 0 && result.stdout.trim() === String(pid)
}

function tcpPortClosed(port: number): Promise<boolean> {
  return new Promise(resolve => {
    const socket = net.createConnection({ host: '127.0.0.1', port })
    socket.once('connect', () => {
      socket.destroy()
      resolve(false)
    })
    socket.once('error', () => resolve(true))
  })
}

async function launchAuthenticatedFixture(
  options: { afterLaunch?(snapshot: FixtureFailureSnapshot): void } = {}
): Promise<AuthenticatedFixture> {
  if (!fs.existsSync(PYTHON)) {
    throw new Error(`Hermes E2E Python is unavailable: ${PYTHON}`)
  }

  let sandbox: Sandbox | undefined
  let server: ControllableAuthServer | undefined
  let mock: MockServer | undefined
  let runtimeRoot: string | undefined
  let launched: Awaited<ReturnType<typeof launchDesktop>> | undefined

  try {
    sandbox = createSandbox('auth-continuity')
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

    server = (await startFixedAuthContractServer({
      certPath,
      keyPath,
      listenPort: 0
    })) as ControllableAuthServer
    mock = await startMockServer()
    writeMockProviderConfig(sandbox.hermesHome, mock.url)
    writeEnvFile(sandbox.hermesHome)

    const credentialStorePath = path.join(sandbox.root, 'auth-credential-store.json')
    const isolation = `auth-continuity-${randomBytes(16).toString('hex')}`
    runtimeRoot = createInstrumentedRuntimeRoot({
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
    launched = await launchDesktop(environment)

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
      cleanup: () => cleanupAuthenticatedResources({ app: fixture.app, mock, runtimeRoot, sandbox, server })
    }

    options.afterLaunch?.({
      appPid: fixture.app.process().pid!,
      mockPort: Number(new URL(mock.url).port),
      root: sandbox.root,
      runtimeRoot,
      serverPort: server.listenPort
    })

    return fixture
  } catch (error) {
    try {
      await cleanupAuthenticatedResources({ app: launched?.app, mock, runtimeRoot, sandbox, server })
    } catch (cleanupError) {
      throw new AggregateError([error, ...aggregateErrors(cleanupError)], 'Fixture construction and rollback failed')
    }

    throw error
  }
}

async function cleanupAuthenticatedResources(resources: {
  app?: ElectronApplication
  mock?: MockServer
  runtimeRoot?: string
  sandbox?: Sandbox
  server?: ControllableAuthServer
}): Promise<void> {
  const errors: unknown[] = []
  if (resources.app) {
    try {
      await closeDesktopApp(resources.app, { timeoutMs: 15_000 })
    } catch (error) {
      errors.push(new Error(`desktop cleanup failed: ${String(error)}`))
    }
  }

  const settled = await Promise.allSettled([
    resources.runtimeRoot ? stopNewAuthOwners(resources.runtimeRoot) : Promise.resolve(),
    resources.mock ? resources.mock.close() : Promise.resolve(),
    resources.server ? resources.server.close() : Promise.resolve()
  ])
  for (const [index, result] of settled.entries()) {
    if (result.status === 'rejected') {
      errors.push(
        new Error(`${['auth owner', 'mock server', 'auth server'][index]} cleanup failed: ${String(result.reason)}`)
      )
    }
  }

  try {
    resources.sandbox?.cleanup()
  } catch (error) {
    errors.push(new Error(`sandbox cleanup failed: ${String(error)}`))
  }

  if (errors.length > 0) {
    throw new AggregateError(errors, 'Authenticated fixture cleanup failed')
  }
}

function aggregateErrors(error: unknown): unknown[] {
  return error instanceof AggregateError ? [...error.errors] : [error]
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

async function createLocalArtifacts(fixture: AuthenticatedFixture, conversationText: string): Promise<void> {
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
  fs.writeFileSync(outboxPath, JSON.stringify({ batch_id: 'auth-continuity-batch', trace: 'preserve' }) + '\n', 'utf8')

  const rootStateDatabase = path.join(fixture.sandbox.hermesHome, 'state.db')
  const projectAndProfile = spawnSync(
    PYTHON,
    [
      '-c',
      [
        'import sys',
        'from pathlib import Path',
        'from hermes_cli.profiles import create_profile',
        'from hermes_cli.projects_db import connect, create_project, list_projects',
        'from hermes_state import SessionDB',
        'home=Path(sys.argv[1])',
        'profile=home/"profiles"/"research"',
        'profile.exists() or create_profile("research", no_skills=True)',
        'profile_db=SessionDB(profile/"state.db")',
        'profile_db.close()',
        'projects=connect(home/"projects.db")',
        'list_projects(projects) or create_project(projects, name="Auth Continuity Project", folders=[sys.argv[2]])',
        'projects.close()'
      ].join('; '),
      fixture.sandbox.hermesHome,
      fixture.sandbox.root
    ],
    {
      cwd: fixture.runtimeRoot,
      encoding: 'utf8',
      env: { ...process.env, HERMES_HOME: fixture.sandbox.hermesHome }
    }
  )
  if (projectAndProfile.status !== 0) {
    throw new Error(`Unable to create authoritative project/profile artifacts: ${projectAndProfile.stderr.trim()}`)
  }

  const exportScript = [
    'import json,sqlite3,sys',
    'from pathlib import Path',
    'from hermes_state import SessionDB',
    'database=Path(sys.argv[1])',
    'needle=sys.argv[2]',
    'conn=sqlite3.connect(f"file:{database}?mode=ro", uri=True)',
    'row=conn.execute("SELECT session_id FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT 1", (f"%{needle}%",)).fetchone()',
    'conn.close()',
    'assert row, "real conversation row not found"',
    'db=SessionDB(database, read_only=True)',
    'payload=db.export_session(row[0])',
    'db.close()',
    'assert payload and any(needle in str(message.get("content") or "") for message in payload["messages"]), "conversation export did not contain sentinel"',
    'Path(sys.argv[3]).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True)+"\\n", encoding="utf-8")'
  ].join('; ')
  const deadline = Date.now() + 10_000
  let exportResult: ReturnType<typeof spawnSync>
  do {
    exportResult = spawnSync(PYTHON, ['-c', exportScript, rootStateDatabase, conversationText, exportPath], {
      cwd: fixture.runtimeRoot,
      encoding: 'utf8',
      env: { ...process.env, HERMES_HOME: fixture.sandbox.hermesHome }
    })
    if (exportResult.status === 0) {
      break
    }
    await new Promise(resolve => setTimeout(resolve, 100))
  } while (Date.now() < deadline)
  if (exportResult.status !== 0) {
    throw new Error(`Unable to query/export the real conversation: ${String(exportResult.stderr).trim()}`)
  }
}

function localDigests(fixture: AuthenticatedFixture): LocalDataEvidence {
  return localDataDigests({
    hermesHome: fixture.sandbox.hermesHome,
    userDataDir: fixture.sandbox.userDataDir,
    python: PYTHON
  })
}

function assertRequiredDigestCoverage(digests: LocalDataEvidence): void {
  const sqliteKeys = Object.keys(digests.sqlite)
  const artifactKeys = Object.keys(digests.artifacts)
  for (const expected of [/hermes\/state\.db$/, /hermes\/projects\.db$/, /hermes\/profiles\/research\/state\.db$/]) {
    expect(
      sqliteKeys.some(key => expected.test(key)),
      `SQLite evidence did not cover ${expected}`
    ).toBe(true)
  }
  for (const expected of [
    /hermes\/kanban\/attachments\/.*attachment-preservation\.bin$/,
    /hermes\/exports\/conversation\.json$/,
    /desktop\/trace-outbox\/.*0000000000000001\.jsonl$/
  ]) {
    expect(
      artifactKeys.some(key => expected.test(key)),
      `Artifact evidence did not cover ${expected}`
    ).toBe(true)
  }
  expect(digests.sqlite['hermes/state.db'].physical.wal).toBeDefined()
  expect(digests.sqlite['hermes/state.db'].physical.shm).toBeDefined()
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
