import { execFileSync } from 'node:child_process'
import fs from 'node:fs'

import { closeDesktopApp, createSandbox, launchInstalledWindowsApp, resolveInstalledWindowsBinary } from './fixtures'
import { allowErrorBanners, expect, test, type ElectronApplication } from './test'
import { descendantsOf, windowsProcessSnapshot } from './windows-process-tree'

test.describe.configure({ timeout: 2 * 60_000, retries: 0 })

test('Playwright drives the installed Windows executable', async () => {
  test.skip(process.platform !== 'win32', 'installed Windows smoke requires win32')
  allowErrorBanners()

  const sandbox = createSandbox('installed-windows-smoke')
  const executablePath = resolveInstalledWindowsBinary()
  let app: ElectronApplication | null = null
  let appPid: number | null = null
  const trackedPids = new Set<number>()
  try {
    const launched = await launchInstalledWindowsApp({ executablePath, sandbox })
    app = launched.app
    const launchedPid = app.process().pid
    expect(launchedPid).toBeDefined()
    appPid = launchedPid!
    await expect(launched.page).toHaveTitle(/Hermes/i)
  } finally {
    try {
      if (app) {
        if (appPid !== null) {
          for (const row of descendantsOf(windowsProcessSnapshot(), appPid)) {
            trackedPids.add(row.pid)
          }
        }
        await closeDesktopApp(app, { timeoutMs: 10_000 })
      }
    } finally {
      try {
        if (appPid !== null) {
          const sandboxPath = sandbox.root.toLowerCase()
          const snapshot = windowsProcessSnapshot()
          for (const row of descendantsOf(snapshot, appPid)) {
            trackedPids.add(row.pid)
          }
          const exactRows = snapshot.filter(
            row => trackedPids.has(row.pid) || row.commandLine.toLowerCase().includes(sandboxPath)
          )
          for (const row of exactRows.reverse()) {
            try {
              execFileSync('powershell.exe', [
                '-NoProfile',
                '-NonInteractive',
                '-Command',
                `Stop-Process -Id ${row.pid} -Force`
              ])
            } catch {}
          }
          await expect
            .poll(
              () =>
                windowsProcessSnapshot()
                  .filter(row => trackedPids.has(row.pid) || row.commandLine.toLowerCase().includes(sandboxPath))
                  .map(row => row.pid),
              { timeout: 10_000, intervals: [250] }
            )
            .toEqual([])
        }
      } finally {
        sandbox.cleanup()
        expect(fs.existsSync(sandbox.root)).toBe(false)
      }
    }
  }
})
