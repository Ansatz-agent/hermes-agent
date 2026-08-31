import { execFile } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'

const MANIFEST_SCHEMA_VERSION = 1
const SOURCE_MARKER_SCHEMA_VERSION = 1
const COMMIT_RE = /^[0-9a-f]{40}$/i
const SHA256_RE = /^[0-9a-f]{64}$/
const SOURCE_MARKER_NAME = '.hermes-bundled-source.json'
const ARCHIVE_FILE = 'hermes-backend.tar.gz'
type DesktopInstallerFile = 'install.sh' | 'install.ps1'

const RUNTIME_SCRIPT_FILES = [
  'desktop-update.ps1',
  'desktop-update/posix.sh',
  'desktop-update/windows.ps1',
  'discord-voice-doctor.py',
  'hermes-gateway',
  'install.cmd',
  'install.ps1',
  'keystroke_diagnostic.py'
]

const RUNTIME_SCRIPT_DIRECTORIES = ['lib', 'whatsapp-bridge']

const REQUIRED_SOURCE_PATHS = [
  'pyproject.toml',
  path.join('hermes_cli', 'main.py'),
  path.join('tools', 'sensevoice_stt.py'),
  'scripts'
]

const FORBIDDEN_ARCHIVE_PREFIXES = [
  'hermes-agent/tests/',
  'hermes-agent/tests-js/',
  'hermes-agent/docs/',
  'hermes-agent/website/',
  'hermes-agent/.github/',
  'hermes-agent/.git/'
]

const TEST_ENTRY_RE =
  /(^|\/)(tests?|__tests__)(\/|$)|\.(test|spec)\.(py|js|mjs|ts|tsx)$|\/test_[^/]*\.py$/

const DECLARATION_TEST_RE = /\.(test|spec)-d\.ts$/
const NESTED_DOCS_RE = /(^|\/)docs(\/|$)/
const GIT_METADATA_RE = /(^|\/)\.(gitignore|gitattributes|gitmodules)$/

export interface BundledPayloadManifest {
  schemaVersion: 1
  commit: string
  branch: string | null
  archive: { file: 'hermes-backend.tar.gz'; size: number; sha256: string }
  installer: { file: DesktopInstallerFile; size: number; sha256: string }
  gitBashRuntime?: {
    file: 'git-bash-runtime.tar.xz'
    size: number
    sha256: string
    entries: number
    source: { file: 'PortableGit-2.55.0.3-64-bit.7z.exe'; sha256: string }
  }
}

export interface BundledPayload {
  manifest: BundledPayloadManifest
  manifestPath: string
  archivePath: string
  installerPath: string
  gitRuntimePath: string | null
}

export interface BundledSourceMarker {
  schemaVersion: 1
  commit: string
  archiveSha256: string
  installedAt: string
}

export interface PreparedBundledSource {
  kind: 'existing-git' | 'fresh' | 'reuse' | 'refresh'
  commit: string
  markerPath: string
  backupPath: string | null
  finalize(): Promise<void>
  rollback(): Promise<void>
}

interface InstallStampLike {
  commit?: unknown
  branch?: unknown
}

interface ResolveBundledPayloadOptions {
  bootstrapRoot: string
  installStamp: InstallStampLike | null | undefined
  targetPlatform?: NodeJS.Platform
}

interface PrepareBundledSourceOptions {
  payload: BundledPayload
  activeRoot: string
  hermesHome: string
}

function execFileText(command: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(command, args, { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`${command} ${args.join(' ')} failed: ${stderr || error.message}`))

        return
      }

      resolve(stdout)
    })
  })
}

function isRealCommit(value: unknown): value is string {
  return typeof value === 'string' && COMMIT_RE.test(value) && !/^0+$/.test(value)
}

function isSha256(value: unknown): value is string {
  return typeof value === 'string' && SHA256_RE.test(value)
}

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function installerFileForPlatform(platform: NodeJS.Platform): DesktopInstallerFile {
  if (platform === 'darwin') {
    return 'install.sh'
  }

  if (platform === 'win32') {
    return 'install.ps1'
  }

  throw new Error(`unsupported bundled payload platform: ${platform}`)
}

function parseManifest(raw: unknown, installerFile: DesktopInstallerFile): BundledPayloadManifest {
  if (!raw || typeof raw !== 'object') {throw new Error('Bundled payload manifest must be an object')}
  const value = raw as Record<string, any>

  if (value.schemaVersion !== MANIFEST_SCHEMA_VERSION) {
    throw new Error(`Bundled payload manifest schemaVersion must be ${MANIFEST_SCHEMA_VERSION}`)
  }

  if (!isRealCommit(value.commit)) {throw new Error('Bundled payload manifest commit is invalid')}

  if (value.branch !== null && typeof value.branch !== 'string') {
    throw new Error('Bundled payload manifest branch must be a string or null')
  }

  if (
    !value.archive ||
    value.archive.file !== ARCHIVE_FILE ||
    !isPositiveSafeInteger(value.archive.size) ||
    !isSha256(value.archive.sha256)
  ) {
    throw new Error('Bundled payload archive metadata is invalid')
  }

  if (
    !value.installer ||
    value.installer.file !== installerFile ||
    !isPositiveSafeInteger(value.installer.size) ||
    !isSha256(value.installer.sha256)
  ) {
    throw new Error('Bundled payload installer metadata is invalid')
  }

  if (installerFile === 'install.ps1') {
    const git = value.gitBashRuntime
    if (
      !git ||
      git.file !== 'git-bash-runtime.tar.xz' ||
      !isPositiveSafeInteger(git.size) ||
      !isSha256(git.sha256) ||
      !isPositiveSafeInteger(git.entries) ||
      git.source?.file !== 'PortableGit-2.55.0.3-64-bit.7z.exe' ||
      !isSha256(git.source?.sha256)
    ) {
      throw new Error('Bundled Git Bash runtime metadata is invalid')
    }
  } else if (value.gitBashRuntime !== undefined) {
    throw new Error('macOS bundled payload must not declare a Windows Git Bash runtime')
  }

  return value as BundledPayloadManifest
}

async function readJson(filePath: string, label: string): Promise<unknown> {
  try {
    return JSON.parse(await fsp.readFile(filePath, 'utf8'))
  } catch (error) {
    throw new Error(`Cannot read ${label} at ${filePath}: ${(error as Error).message}`)
  }
}

async function requireRegularFile(filePath: string, label: string): Promise<fs.Stats> {
  let stats: fs.Stats

  try {
    stats = await fsp.lstat(filePath)
  } catch (error) {
    throw new Error(`Missing ${label} at ${filePath}: ${(error as Error).message}`)
  }

  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }

  return stats
}

async function sha256File(filePath: string): Promise<string> {
  return createHash('sha256').update(await fsp.readFile(filePath)).digest('hex')
}

export function archiveEntryIsSafe(entry: string): boolean {
  if (!entry || entry.includes('\\') || entry.includes('\0') || path.posix.isAbsolute(entry)) {return false}
  const normalized = path.posix.normalize(entry)

  if (normalized === 'hermes-agent' || normalized === 'hermes-agent/') {return true}

  return normalized.startsWith('hermes-agent/') && !normalized.split('/').includes('..')
}

function archiveEntryIsForbidden(entry: string): boolean {
  return (
    FORBIDDEN_ARCHIVE_PREFIXES.some(prefix => entry.startsWith(prefix)) ||
    TEST_ENTRY_RE.test(entry) ||
    DECLARATION_TEST_RE.test(entry) ||
    NESTED_DOCS_RE.test(entry) ||
    GIT_METADATA_RE.test(entry) ||
    !runtimeScriptEntryIsAllowed(entry)
  )
}

function runtimeScriptEntryIsAllowed(entry: string): boolean {
  const prefix = 'hermes-agent/scripts'
  const normalized = entry.replace(/\/$/, '')

  if (normalized === prefix) {return true}

  if (!normalized.startsWith(`${prefix}/`)) {return true}

  const relative = normalized.slice(prefix.length + 1)

  if (RUNTIME_SCRIPT_FILES.includes(relative)) {return true}

  if (RUNTIME_SCRIPT_FILES.some(filePath => filePath.startsWith(`${relative}/`))) {return true}

  return RUNTIME_SCRIPT_DIRECTORIES.some(
    directory =>
      relative === directory ||
      relative.startsWith(`${directory}/`) ||
      directory.startsWith(`${relative}/`)
  )
}

async function verifyArchiveEntries(archivePath: string): Promise<void> {
  const namesOutput = await execFileText('tar', ['-tzf', archivePath])
  const entries = namesOutput.split(/\r?\n/).filter(Boolean)

  if (entries.length === 0) {throw new Error('Bundled backend archive is empty')}

  for (const entry of entries) {
    if (!archiveEntryIsSafe(entry) || archiveEntryIsForbidden(entry)) {
      throw new Error(`Bundled backend archive contains unsafe entry: ${entry}`)
    }
  }

  const verboseOutput = await execFileText('tar', ['-tvzf', archivePath])

  for (const line of verboseOutput.split(/\r?\n/).filter(Boolean)) {
    const entryType = line[0]

    if (entryType !== '-' && entryType !== 'd') {
      throw new Error(`Bundled backend archive contains unsupported link or entry type: ${line}`)
    }
  }
}

export async function resolveBundledPayload({
  bootstrapRoot,
  installStamp,
  targetPlatform = process.platform
}: ResolveBundledPayloadOptions): Promise<BundledPayload> {
  const installerFile = installerFileForPlatform(targetPlatform)
  const manifestPath = path.join(bootstrapRoot, 'payload-manifest.json')
  const manifest = parseManifest(await readJson(manifestPath, 'bundled payload manifest'), installerFile)

  if (!isRealCommit(installStamp?.commit) || manifest.commit !== installStamp.commit) {
    throw new Error(
      `Bundled payload commit ${manifest.commit} does not match install stamp commit ${String(installStamp?.commit)}`
    )
  }

  const archivePath = path.join(bootstrapRoot, ARCHIVE_FILE)
  const installerPath = path.join(bootstrapRoot, installerFile)
  const gitRuntimePath = manifest.gitBashRuntime
    ? path.join(bootstrapRoot, manifest.gitBashRuntime.file)
    : null
  const archiveStats = await requireRegularFile(archivePath, 'bundled backend archive')
  const installerStats = await requireRegularFile(installerPath, 'bundled installer')
  const gitRuntimeStats = gitRuntimePath
    ? await requireRegularFile(gitRuntimePath, 'bundled Git Bash runtime')
    : null

  if (archiveStats.size !== manifest.archive.size) {
    throw new Error(`Bundled backend archive size mismatch: expected ${manifest.archive.size}, got ${archiveStats.size}`)
  }

  if (installerStats.size !== manifest.installer.size) {
    throw new Error(`Bundled installer size mismatch: expected ${manifest.installer.size}, got ${installerStats.size}`)
  }
  if (gitRuntimeStats && gitRuntimeStats.size !== manifest.gitBashRuntime?.size) {
    throw new Error('Bundled Git Bash runtime size mismatch')
  }

  const [archiveSha256, installerSha256, gitRuntimeSha256] = await Promise.all([
    sha256File(archivePath),
    sha256File(installerPath),
    gitRuntimePath ? sha256File(gitRuntimePath) : Promise.resolve(null)
  ])

  if (archiveSha256 !== manifest.archive.sha256) {
    throw new Error('Bundled backend archive SHA-256 checksum mismatch')
  }

  if (installerSha256 !== manifest.installer.sha256) {
    throw new Error('Bundled installer SHA-256 checksum mismatch')
  }
  if (manifest.gitBashRuntime && gitRuntimeSha256 !== manifest.gitBashRuntime.sha256) {
    throw new Error('Bundled Git Bash runtime SHA-256 checksum mismatch')
  }

  await verifyArchiveEntries(archivePath)

  return { manifest, manifestPath, archivePath, installerPath, gitRuntimePath }
}

function parseSourceMarker(raw: unknown): BundledSourceMarker | null {
  if (!raw || typeof raw !== 'object') {return null}
  const value = raw as Record<string, unknown>

  if (
    value.schemaVersion !== SOURCE_MARKER_SCHEMA_VERSION ||
    !isRealCommit(value.commit) ||
    !isSha256(value.archiveSha256) ||
    typeof value.installedAt !== 'string'
  ) {
    return null
  }

  return value as unknown as BundledSourceMarker
}

export function readBundledSourceMarker(activeRoot: string): BundledSourceMarker | null {
  try {
    return parseSourceMarker(JSON.parse(fs.readFileSync(path.join(activeRoot, SOURCE_MARKER_NAME), 'utf8')))
  } catch {
    return null
  }
}

async function sourceTreeIsComplete(sourceRoot: string): Promise<boolean> {
  for (const relativePath of REQUIRED_SOURCE_PATHS) {
    try {
      const stats = await fsp.stat(path.join(sourceRoot, relativePath))

      if (relativePath === 'scripts' ? !stats.isDirectory() : !stats.isFile()) {return false}
    } catch {
      return false
    }
  }

  return true
}

async function writeSourceMarker(sourceRoot: string, marker: BundledSourceMarker): Promise<string> {
  const markerPath = path.join(sourceRoot, SOURCE_MARKER_NAME)
  const temporaryPath = `${markerPath}.tmp-${process.pid}-${randomUUID()}`
  await fsp.writeFile(temporaryPath, JSON.stringify(marker, null, 2) + '\n', 'utf8')
  await fsp.rename(temporaryPath, markerPath)

  return markerPath
}

function assertManagedDestination(activeRoot: string, hermesHome: string): void {
  const resolvedRoot = path.resolve(activeRoot)
  const resolvedHome = path.resolve(hermesHome)
  const relative = path.relative(resolvedHome, resolvedRoot)

  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`Bundled source destination must be a child of Hermes home: ${resolvedRoot}`)
  }
}

export function bundledSourceBackupPath(activeRoot: string): string {
  const resolvedRoot = path.resolve(activeRoot)

  return path.join(path.dirname(resolvedRoot), `.${path.basename(resolvedRoot)}-bundled-backup`)
}

export function bundledSourceCleanupTombstonePrefix(activeRoot: string): string {
  return `${bundledSourceBackupPath(activeRoot)}.deleting-`
}

async function assertCompleteManagedBackup(backupPath: string): Promise<void> {
  if (!fs.existsSync(backupPath)) {
    throw new Error(`Cannot roll back bundled source: backup is missing at ${backupPath}`)
  }

  if (!readBundledSourceMarker(backupPath) || !(await sourceTreeIsComplete(backupPath))) {
    throw new Error(`Refusing bundled-source recovery: backup is not a complete managed bundled source at ${backupPath}`)
  }
}

async function removeTreeBestEffort(treePath: string): Promise<void> {
  try {
    await fsp.rm(treePath, { recursive: true, force: true })
  } catch (error) {
    console.warn(`Could not remove retired bundled-source tree ${treePath}: ${(error as Error).message}`)
  }
}

async function restoreManagedBackup(activeRoot: string, backupPath: string): Promise<void> {
  await assertCompleteManagedBackup(backupPath)
  let displacedPath: string | null = null

  if (fs.existsSync(activeRoot)) {
    displacedPath = `${activeRoot}.rollback-${process.pid}-${randomUUID()}`
    await fsp.rename(activeRoot, displacedPath)
  }

  try {
    await fsp.rename(backupPath, activeRoot)
  } catch (error) {
    if (displacedPath && !fs.existsSync(activeRoot)) {
      await fsp.rename(displacedPath, activeRoot)
    }

    throw error
  }

  if (displacedPath) {await removeTreeBestEffort(displacedPath)}
}

function noOpTransaction(
  kind: 'existing-git' | 'reuse',
  payload: BundledPayload,
  activeRoot: string
): PreparedBundledSource {
  return {
    kind,
    commit: payload.manifest.commit,
    markerPath: path.join(activeRoot, SOURCE_MARKER_NAME),
    backupPath: null,
    async finalize() {},
    async rollback() {}
  }
}

function managedSourceTransaction(
  kind: 'fresh' | 'refresh',
  payload: BundledPayload,
  activeRoot: string,
  backupPath: string | null
): PreparedBundledSource {
  let settled = false

  const finalize = async (): Promise<void> => {
    if (settled) {return}

    if (backupPath) {
      await assertCompleteManagedBackup(backupPath)
      const tombstonePath = `${bundledSourceCleanupTombstonePrefix(activeRoot)}${process.pid}-${randomUUID()}`
      await fsp.rename(backupPath, tombstonePath)
      settled = true
      await removeTreeBestEffort(tombstonePath)

      return
    }

    settled = true
  }

  const rollback = async (): Promise<void> => {
    if (settled) {return}

    if (backupPath) {
      // Validate ownership before touching either directory. The active tree
      // is atomically displaced rather than recursively deleted, so a process
      // death leaves the durable backup recoverable on the next launch.
      await restoreManagedBackup(activeRoot, backupPath)
    } else {
      if (fs.existsSync(activeRoot)) {
        const tombstonePath = `${activeRoot}.rollback-${process.pid}-${randomUUID()}`
        await fsp.rename(activeRoot, tombstonePath)
        settled = true
        await removeTreeBestEffort(tombstonePath)

        return
      }
    }

    settled = true
  }

  return {
    kind,
    commit: payload.manifest.commit,
    markerPath: path.join(activeRoot, SOURCE_MARKER_NAME),
    backupPath,
    finalize,
    rollback
  }
}

export async function prepareBundledSource({
  payload,
  activeRoot,
  hermesHome
}: PrepareBundledSourceOptions): Promise<PreparedBundledSource> {
  assertManagedDestination(activeRoot, hermesHome)
  const durableBackupPath = bundledSourceBackupPath(activeRoot)
  let activeExists = fs.existsSync(activeRoot)

  if (fs.existsSync(durableBackupPath)) {
    await assertCompleteManagedBackup(durableBackupPath)

    if (!activeExists) {
      // The process stopped after moving the live runtime aside but before
      // promoting staging. Restore first, then start a new verified refresh.
      await fsp.rename(durableBackupPath, activeRoot)
      activeExists = true
    } else {
      const promotedMarker = readBundledSourceMarker(activeRoot)

      const promotedMatchesPayload =
        promotedMarker?.commit === payload.manifest.commit &&
        promotedMarker.archiveSha256 === payload.manifest.archive.sha256

      if (promotedMatchesPayload && (await sourceTreeIsComplete(activeRoot))) {
        // A previous process promoted the new tree and then exited before it
        // could finalize or roll back. Adopt its durable backup so this run
        // retains both options through dependency installation and smoke test.
        return managedSourceTransaction('refresh', payload, activeRoot, durableBackupPath)
      }

      if (promotedMatchesPayload) {
        // The promoted tree is recognizably ours but incomplete. Restore the
        // known-good runtime before attempting a fresh extraction.
        await restoreManagedBackup(activeRoot, durableBackupPath)
        activeExists = true
      } else {
        throw new Error(
          `Refusing bundled-source recovery: both active source and pending backup exist at ${activeRoot}`
        )
      }
    }
  }

  if (activeExists && fs.existsSync(path.join(activeRoot, '.git'))) {
    return noOpTransaction('existing-git', payload, activeRoot)
  }

  const existingMarker = activeExists ? readBundledSourceMarker(activeRoot) : null

  if (activeExists && !existingMarker) {
    throw new Error(`Refusing to replace ${activeRoot}: it is not a Git checkout or managed bundled source`)
  }

  if (existingMarker && !(await sourceTreeIsComplete(activeRoot))) {
    throw new Error(`Refusing to replace incomplete managed bundled source at ${activeRoot}`)
  }

  if (
    existingMarker &&
    existingMarker.commit === payload.manifest.commit &&
    existingMarker.archiveSha256 === payload.manifest.archive.sha256 &&
    (await sourceTreeIsComplete(activeRoot))
  ) {
    return noOpTransaction('reuse', payload, activeRoot)
  }

  const parent = path.dirname(activeRoot)
  await fsp.mkdir(parent, { recursive: true })
  const stagingPath = await fsp.mkdtemp(path.join(parent, '.hermes-agent-staging-'))
  let backupPath: string | null = null
  const kind: 'fresh' | 'refresh' = activeExists ? 'refresh' : 'fresh'

  try {
    await execFileText('tar', [
      '-xzf',
      payload.archivePath,
      '-C',
      stagingPath,
      '--strip-components',
      '1'
    ])

    if (!(await sourceTreeIsComplete(stagingPath))) {
      throw new Error('Bundled backend is missing required runtime file tools/sensevoice_stt.py or another core path')
    }

    const scriptsDir = path.join(stagingPath, 'scripts')
    await fsp.mkdir(scriptsDir, { recursive: true })
    await fsp.copyFile(payload.installerPath, path.join(scriptsDir, payload.manifest.installer.file))

    const marker: BundledSourceMarker = {
      schemaVersion: SOURCE_MARKER_SCHEMA_VERSION,
      commit: payload.manifest.commit,
      archiveSha256: payload.manifest.archive.sha256,
      installedAt: new Date().toISOString()
    }

    await writeSourceMarker(stagingPath, marker)

    if (activeExists) {
      if (fs.existsSync(durableBackupPath)) {
        throw new Error(`Bundled source backup already exists at ${durableBackupPath}`)
      }

      backupPath = durableBackupPath
      await fsp.rename(activeRoot, backupPath)
    }

    try {
      await fsp.rename(stagingPath, activeRoot)
    } catch (error) {
      if (backupPath && !fs.existsSync(activeRoot)) {await fsp.rename(backupPath, activeRoot)}
      throw error
    }
  } catch (error) {
    await fsp.rm(stagingPath, { recursive: true, force: true })
    throw error
  }

  return managedSourceTransaction(kind, payload, activeRoot, backupPath)
}
