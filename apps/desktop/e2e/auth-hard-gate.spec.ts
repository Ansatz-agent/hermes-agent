import { spawnSync } from 'node:child_process'

import { buildAppEnv, createSandbox, launchDesktop, type Sandbox } from './fixtures'
import { allowErrorBanners, type ElectronApplication, expect, type Page, test } from './test'

let app: ElectronApplication | null = null
let page: Page | null = null
let sandbox: Sandbox | null = null
let authOwnerPidsBefore = new Set<number>()

// Playwright 1.58 on Node 26 leaves the Electron trace writer's FSEvents
// handle open after the app has exited. This spec already captures a final
// screenshot and asserts every protected surface directly; disabling trace
// keeps teardown deterministic without reducing the hard-gate assertions.
test.use({ trace: 'off' })

test.beforeAll(async () => {
  if (process.platform !== 'win32') {
    authOwnerPidsBefore = authOwnerPids()
  }

  sandbox = createSandbox('auth-hard-gate')

  const launched = await launchDesktop(
    buildAppEnv(sandbox, {
      // The auth runtime deliberately uses its non-persistent MemoryOwner in
      // SSH/headless mode. This gives the test a deterministic signed-out
      // process without reading or mutating the developer's OS keychain.
      SSH_CONNECTION: '127.0.0.1 40000 127.0.0.1 22'
    })
  )

  app = launched.app
  page = launched.page
})

test.afterAll(async () => {
  try {
    if (app) {
      const child = app.process()

      // Playwright's close path closes renderer windows, which follows the
      // normal macOS convention of leaving a windowless app alive. Schedule a
      // production app.quit() after this evaluate RPC has returned, then start
      // Playwright's close while the transport is still alive so BrowserContext
      // resources are finalized before the process exits.
      await app.evaluate(({ app: electronApp }) => {
        // Leave enough time for the CDP evaluate response to reach Playwright;
        // setImmediate can win that race and strand the caller during teardown.
        setTimeout(() => electronApp.quit(), 500)

        return true
      })

      const graceful = await Promise.race([
        app.close().then(() => true),
        new Promise<boolean>(resolve => setTimeout(() => resolve(false), 10_000))
      ])

      if (!graceful) {
        child.kill('SIGKILL')
        throw new Error('Playwright did not close Desktop within 10 seconds of app.quit()')
      }
    }
  } finally {
    if (process.platform !== 'win32') {
      await stopAuthOwnersStartedBySpec(authOwnerPidsBefore)
    }

    sandbox?.cleanup()
    app = null
    page = null
    sandbox = null
    authOwnerPidsBefore = new Set<number>()
  }
})

test('unauthenticated startup exposes only account login and rejects every capability boundary', async () => {
  // The signed-out reason is intentionally rendered as an accessible alert.
  allowErrorBanners()

  await expect(page!.locator('main section h1')).toContainText('Hermes')
  await expect(page!.getByText('https://c2sml.cn/auth')).toBeVisible()
  // Initial online verification has its own 5s deadline. Give the UI enough
  // room to transition from "checking" to the signed-out form on a cold CI
  // start instead of racing Playwright's default 5s assertion timeout.
  await expect(page!.locator('input[name="username"]')).toBeVisible({ timeout: 15_000 })
  await expect(page!.locator('input[name="password"]')).toHaveAttribute('type', 'password')
  await expect(page!.locator('main section button')).toHaveCount(1)
  await expect(page!.locator('main section button[type="submit"]')).toBeVisible()
  await expect(page!.getByRole('button', { name: /retry|重试|重試|再試行/i })).toHaveCount(0)
  await expect(page!.locator('main section > p').last()).toContainText(/administrator|管理员|管理者|管理員/i)
  await expect(page!.getByRole('button', { name: /account|账户/i })).toHaveCount(0)
  await expect(page!.getByText(/sign out|退出登录/i)).toHaveCount(0)

  const body = page!.locator('body')

  await expect(body).not.toContainText('Setting up Hermes Agent')

  for (const forbidden of [
    'Register',
    'Create account',
    'Invitation',
    'Reset password',
    'Change password',
    'Insecure TLS',
    'Offline',
    'Skip',
    '注册',
    '创建账户',
    '邀请',
    '重置密码',
    '修改密码',
    '离线',
    '跳过'
  ]) {
    await expect(body).not.toContainText(forbidden)
  }

  await expect(page!.locator('main section a, main section select, main section input[type="checkbox"]')).toHaveCount(0)
  await expect(page!.locator('main section input')).toHaveCount(2)

  await expect(page!.locator('textarea, [contenteditable="true"], [data-terminal-slot]')).toHaveCount(0)

  const protectedRendererLoaded = await page!.evaluate(() =>
    performance.getEntriesByType('resource').some(entry => /(?:^|\/)protected-root-[^/]+\.js(?:$|\?)/.test(entry.name))
  )

  expect(protectedRendererLoaded).toBe(false)

  const rejections = await page!.evaluate(async () => {
    const desktop = (window as any).hermesDesktop

    const attempt = async (operation: () => Promise<unknown>) => {
      try {
        await operation()

        return 'ALLOWED'
      } catch (error) {
        return String(error)
      }
    }

    return Promise.all([
      attempt(() => desktop.getConnection()),
      attempt(() => desktop.terminal.start({ cwd: '/' })),
      attempt(() => desktop.signalDeepLinkReady()),
      attempt(() => desktop.hud.open()),
      attempt(() => desktop.quickEntry.getSettings()),
      attempt(() => desktop.petOverlay.open({ bounds: { height: 100, width: 100, x: 0, y: 0 } }))
    ])
  })

  expect(rejections).toHaveLength(6)

  for (const rejection of rejections) {
    expect(rejection).not.toBe('ALLOWED')
    expect(rejection).toContain('AUTH_REQUIRED')
    expect(rejection).not.toContain('sessionid')
  }

  const windows = await app!.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows().map(
      (window: { isDestroyed: () => boolean; webContents: { getURL: () => string } }) => ({
        destroyed: window.isDestroyed(),
        url: window.webContents.getURL()
      })
    )
  )

  expect(windows).toHaveLength(1)
  expect(windows[0]).toMatchObject({ destroyed: false })
  expect(windows[0]?.url).not.toMatch(/[?&]win=(?:hud|overlay|quick|wake)(?:&|$)/)

  if (process.platform !== 'win32') {
    const rootPid = app!.process().pid
    expect(rootPid).toBeDefined()
    const commands = descendantCommands(rootPid!)

    expect(commands.some(command => /hermes_cli\.main.*\b(?:serve|dashboard)\b/i.test(command))).toBe(false)
    expect(commands.some(command => /(?:^|\s)hermes(?:\.exe)?\s+(?:serve|dashboard)\b/i.test(command))).toBe(false)
  }
})

function descendantCommands(rootPid: number): string[] {
  const result = spawnSync('ps', ['-axo', 'pid=,ppid=,command='], { encoding: 'utf8' })

  if (result.status !== 0) {
    throw new Error('Unable to inspect the Electron process tree')
  }

  const rows = result.stdout
    .split('\n')
    .map(line => line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/))
    .filter((match): match is RegExpMatchArray => Boolean(match))
    .map(match => ({ command: match[3]!, parent: Number(match[2]), pid: Number(match[1]) }))

  const descendants = new Set([rootPid])
  let changed = true

  while (changed) {
    changed = false

    for (const row of rows) {
      if (descendants.has(row.parent) && !descendants.has(row.pid)) {
        descendants.add(row.pid)
        changed = true
      }
    }
  }

  return rows.filter(row => row.pid !== rootPid && descendants.has(row.pid)).map(row => row.command)
}

function authOwnerPids(): Set<number> {
  const result = spawnSync('ps', ['-axo', 'pid=,command='], { encoding: 'utf8' })

  if (result.status !== 0) {
    throw new Error('Unable to inspect auth owner processes')
  }

  return new Set(
    result.stdout
      .split('\n')
      .map(line => line.trim().match(/^(\d+)\s+(.+)$/))
      .filter((match): match is RegExpMatchArray => Boolean(match))
      .filter(match => /(?:^|\s)-m\s+hermes_cli\.client_auth\.runtime\s+owner(?:\s|$)/.test(match[2]!))
      .map(match => Number(match[1]))
  )
}

async function stopAuthOwnersStartedBySpec(existing: Set<number>): Promise<void> {
  const created = [...authOwnerPids()].filter(pid => !existing.has(pid))

  for (const pid of created) {
    try {
      process.kill(pid, 'SIGTERM')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ESRCH') {
        throw error
      }
    }
  }

  const deadline = Date.now() + 5_000

  while (created.some(pid => authOwnerPids().has(pid)) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50))
  }

  const survivors = created.filter(pid => authOwnerPids().has(pid))

  for (const pid of survivors) {
    process.kill(pid, 'SIGKILL')
  }

  if (survivors.length > 0) {
    throw new Error(`Auth owner teardown required SIGKILL for PIDs: ${survivors.join(', ')}`)
  }
}
