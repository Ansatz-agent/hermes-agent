import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import type { ElectronApplication, Page, Request } from '@playwright/test'

export type AuthStatus = {
  state: 'checking' | 'authenticated' | 'signed_out' | 'locked'
  username: string | null
  account_id: string | null
  session_id: string | null
  installation_id: string | null
  principal_key: string | null
  runtime_instance_id: string
  epoch: number
  valid_until: number
  validation_state: 'unknown' | 'validating' | 'online' | 'degraded'
  validation_reason: string | null
  last_validated_at: string | null
  legacy: boolean
  reason: string | null
  runtime_ready: boolean
}

export async function protectedIpcRejections(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const desktop = (
      window as unknown as {
        hermesDesktop: {
          getConnection: () => Promise<unknown>
          terminal: { start: (options: { cwd: string }) => Promise<unknown> }
          signalDeepLinkReady: () => Promise<unknown>
          hud?: { open: () => Promise<unknown> }
          quickEntry: { getSettings: () => Promise<unknown> }
          petOverlay: {
            open: (options: { bounds: { height: number; width: number; x: number; y: number } }) => Promise<unknown>
          }
        }
      }
    ).hermesDesktop
    const attempt = async (operation: () => Promise<unknown>): Promise<string> => {
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
      attempt(() => desktop.hud!.open()),
      attempt(() => desktop.quickEntry.getSettings()),
      attempt(() => desktop.petOverlay.open({ bounds: { height: 100, width: 100, x: 0, y: 0 } }))
    ])
  })
}

export async function authStatus(page: Page): Promise<AuthStatus> {
  return page.evaluate(() =>
    (
      window as unknown as {
        hermesDesktop: {
          auth: {
            status: () => Promise<AuthStatus>
          }
        }
      }
    ).hermesDesktop.auth.status()
  )
}

export function backendProcessIds(app: ElectronApplication): number[] {
  const rootPid = app.process().pid
  if (!rootPid || process.platform === 'win32') {
    return []
  }
  const result = spawnSync('ps', ['-axo', 'pid=,ppid=,command='], { encoding: 'utf8' })
  if (result.status !== 0) {
    throw new Error('Unable to inspect the Electron process tree')
  }
  const rows = result.stdout
    .split('\n')
    .map(line => line.trim().match(/^(\d+)\s+(\d+)\s+(.+)$/))
    .filter((match): match is RegExpMatchArray => Boolean(match))
    .map(match => ({ pid: Number(match[1]), parent: Number(match[2]), command: match[3]! }))
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
  return rows
    .filter(row => descendants.has(row.pid) && /hermes_cli\.main.*\bserve\b/i.test(row.command))
    .map(row => row.pid)
    .sort((left, right) => left - right)
}

export interface LocalDataDigestOptions {
  hermesHome: string
  userDataDir: string
  python: string
}

/**
 * Hash only explicit user-data allowlists. Credential stores are deliberately
 * absent. SQLite hashes are logical dumps, so an ordinary WAL checkpoint or
 * WAL/SHM removal during backend shutdown cannot masquerade as data loss.
 */
export function localDataDigests(options: LocalDataDigestOptions): Record<string, string> {
  const digests = new Map<string, string>()
  const databasePaths = [path.join(options.hermesHome, 'state.db'), path.join(options.hermesHome, 'projects.db')]
  const profilesRoot = path.join(options.hermesHome, 'profiles')
  if (fs.existsSync(profilesRoot)) {
    for (const profile of fs.readdirSync(profilesRoot).sort()) {
      const profileRoot = path.join(profilesRoot, profile)
      if (!fs.lstatSync(profileRoot).isDirectory()) {
        continue
      }
      databasePaths.push(path.join(profileRoot, 'state.db'), path.join(profileRoot, 'projects.db'))
    }
  }

  for (const databasePath of databasePaths) {
    if (fs.existsSync(databasePath)) {
      digests.set(reportPath(options, databasePath) + '#logical', logicalSqliteDigest(databasePath, options.python))
    }
  }

  const byteRoots = [
    path.join(options.hermesHome, 'kanban', 'attachments'),
    path.join(options.hermesHome, 'exports'),
    path.join(options.userDataDir, 'trace-outbox')
  ]
  if (fs.existsSync(profilesRoot)) {
    for (const profile of fs.readdirSync(profilesRoot).sort()) {
      byteRoots.push(path.join(profilesRoot, profile, 'exports'))
    }
  }
  for (const root of byteRoots) {
    collectByteDigests(root, options, digests)
  }

  const result = Object.fromEntries([...digests.entries()].sort(([left], [right]) => left.localeCompare(right)))
  for (const key of Object.keys(result)) {
    if (/(?:credential|keyring|auth-credential-store|(?:^|\/)auth\.json$|(?:^|\/)\.env$)/i.test(key)) {
      throw new Error(`Credential-store path entered local-data digest report: ${key}`)
    }
  }
  return result
}

export function assertSensitiveValuesAbsent(logRoots: string[], sensitiveValues: readonly string[]): void {
  for (const root of logRoots) {
    for (const filePath of explicitTextLogFiles(root)) {
      const contents = fs.readFileSync(filePath, 'utf8')
      for (const sensitive of sensitiveValues) {
        if (sensitive && contents.includes(sensitive)) {
          throw new Error(`Sensitive auth value leaked into ${filePath}`)
        }
      }
    }
  }
}

function logicalSqliteDigest(databasePath: string, python: string): string {
  const script = [
    'import hashlib, sqlite3, sys',
    'path = sys.argv[1]',
    'connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)',
    'connection.execute("PRAGMA query_only=ON")',
    'dump = "\\n".join(connection.iterdump()).encode("utf-8")',
    'connection.close()',
    'print(hashlib.sha256(dump).hexdigest())'
  ].join('; ')
  const result = spawnSync(python, ['-c', script, databasePath], { encoding: 'utf8' })
  if (result.status !== 0) {
    throw new Error(`Unable to create logical SQLite digest for ${databasePath}: ${result.stderr.trim()}`)
  }
  return result.stdout.trim()
}

function collectByteDigests(root: string, options: LocalDataDigestOptions, output: Map<string, string>): void {
  if (!fs.existsSync(root)) {
    return
  }
  const stats = fs.lstatSync(root)
  if (stats.isSymbolicLink()) {
    return
  }
  if (stats.isDirectory()) {
    for (const entry of fs.readdirSync(root).sort()) {
      collectByteDigests(path.join(root, entry), options, output)
    }
    return
  }
  if (stats.isFile()) {
    output.set(reportPath(options, root), createHash('sha256').update(fs.readFileSync(root)).digest('hex'))
  }
}

function reportPath(options: LocalDataDigestOptions, filePath: string): string {
  const hermesRelative = path.relative(options.hermesHome, filePath)
  if (hermesRelative !== '..' && !hermesRelative.startsWith(`..${path.sep}`)) {
    return `hermes/${hermesRelative.split(path.sep).join('/')}`
  }
  const userDataRelative = path.relative(options.userDataDir, filePath)
  if (userDataRelative !== '..' && !userDataRelative.startsWith(`..${path.sep}`)) {
    return `desktop/${userDataRelative.split(path.sep).join('/')}`
  }
  throw new Error(`Digest target is outside explicit data roots: ${filePath}`)
}

function explicitTextLogFiles(root: string): string[] {
  if (!fs.existsSync(root)) {
    return []
  }
  const stats = fs.lstatSync(root)
  if (stats.isSymbolicLink()) {
    return []
  }
  if (stats.isDirectory()) {
    return fs.readdirSync(root).flatMap(entry => explicitTextLogFiles(path.join(root, entry)))
  }
  return stats.isFile() && ['.json', '.log', '.txt'].includes(path.extname(root).toLowerCase()) ? [root] : []
}

export function trackProtectedRendererChunk(page: Page): () => boolean {
  let requested = false

  const onRequest = (request: Request) => {
    if (/(?:^|\/)protected-root-[^/]+\.js(?:$|[?#])/.test(request.url())) {
      requested = true
    }
  }

  page.on('request', onRequest)

  return () => requested
}
