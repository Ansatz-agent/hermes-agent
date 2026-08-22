import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { listPackage } from '@electron/asar'

import {
  auditGitRuntimeArchive,
  WINDOWS_GIT_RUNTIME_FILE,
  WINDOWS_PORTABLE_GIT_RELEASE_FILE,
  WINDOWS_PORTABLE_GIT_RELEASE_SHA256
} from './prepare-windows-git-runtime.mjs'
import { isMain } from './utils.mjs'

const COMMIT_RE = /^[0-9a-f]{40}$/i
const SHA256_RE = /^[0-9a-f]{64}$/
const PRODUCTION_TEST_COMMAND = 'hermes-agent/hermes_cli/approvals_test.py'
const PRODUCTION_WAKE_MODELS = new Set([
  'hermes-agent/tools/wakewords/hey_hermes.onnx',
  'hermes-agent/tools/wakewords/hey_hermes.tflite'
])
const REQUIRED_ARCHIVE_ENTRIES = Object.freeze([
  'hermes-agent/pyproject.toml',
  'hermes-agent/hermes_cli/main.py',
  'hermes-agent/hermes_cli/client_auth/__init__.py',
  'hermes-agent/hermes_cli/client_auth/bridge.py',
  'hermes-agent/hermes_cli/client_auth/client.py',
  'hermes-agent/hermes_cli/client_auth/entrypoint_wrappers.py',
  'hermes-agent/hermes_cli/client_auth/entrypoints.json',
  'hermes-agent/hermes_cli/client_auth/guard.py',
  'hermes-agent/hermes_cli/client_auth/runtime.py',
  'hermes-agent/hermes_cli/client_auth/static_help.txt',
  'hermes-agent/tools/sensevoice_stt.py',
  'hermes-agent/web/package.json',
  'hermes-agent/ui-tui/package.json',
  'hermes-agent/apps/shared/package.json'
])

function sha256File(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function readJson(filePath, label) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch (error) {
    throw new Error(`Cannot read ${label} at ${filePath}: ${error.message}`)
  }
}

function requireRegularFile(filePath, label) {
  let stats
  try {
    stats = fs.lstatSync(filePath)
  } catch (error) {
    throw new Error(`Missing ${label} at ${filePath}: ${error.message}`)
  }
  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw new Error(`${label} must be a regular non-link file: ${filePath}`)
  }
  return stats
}

function isRealCommit(value) {
  return typeof value === 'string' && COMMIT_RE.test(value) && !/^0+$/.test(value)
}

function normalizeEntry(entry) {
  return String(entry)
    .replace(/^[/\\]+/, '')
    .replaceAll('\\', '/')
}

export function packagedEntryIsForbidden(rawEntry) {
  const entry = normalizeEntry(rawEntry).replace(/\/$/, '')
  if (entry === PRODUCTION_TEST_COMMAND || PRODUCTION_WAKE_MODELS.has(entry)) return false

  const isLocalMemory = /^(?:hermes-agent\/)?memory(?:\/|$)/i.test(entry)

  return (
    isLocalMemory ||
    /(^|\/)(tests?|__tests__|docs?|website)(\/|$)/i.test(entry) ||
    /(^|\/)(test_[^/]*|[^/]*_test)\.py$/i.test(entry) ||
    /\.(test|spec)\.(py|js|mjs|cjs|ts|tsx)$/i.test(entry) ||
    /\.(test|spec)-d\.ts$/i.test(entry) ||
    /(^|\/)\.(git|gitignore|gitattributes|gitmodules)(\/|$)/i.test(entry) ||
    entry.startsWith('hermes-agent/apps/desktop/') ||
    entry.startsWith('hermes-agent/apps/bootstrap-installer/') ||
    /\.(bin|gguf|onnx|safetensors|tflite|pt|pth)$/i.test(entry)
  )
}

function assertSafeArchiveEntry(entry) {
  if (!entry || entry.includes('\\') || entry.includes('\0') || path.posix.isAbsolute(entry)) {
    throw new Error(`Backend archive contains unsafe entry: ${entry}`)
  }
  const normalized = path.posix.normalize(entry)
  if (
    normalized !== 'hermes-agent' &&
    normalized !== 'hermes-agent/' &&
    (!normalized.startsWith('hermes-agent/') || normalized.split('/').includes('..'))
  ) {
    throw new Error(`Backend archive contains unsafe entry: ${entry}`)
  }
}

function validateManifest(manifest, stamp, expectedPortableGitSourceSha256) {
  if (!manifest || typeof manifest !== 'object' || manifest.schemaVersion !== 1) {
    throw new Error('Payload manifest schemaVersion must be 1')
  }
  if (!isRealCommit(manifest.commit) || manifest.commit !== stamp.commit) {
    throw new Error('Payload manifest commit does not match install stamp')
  }
  if (manifest.branch !== null && typeof manifest.branch !== 'string') {
    throw new Error('Payload manifest branch must be a string or null')
  }
  for (const [label, metadata, expectedFile] of [
    ['archive', manifest.archive, 'hermes-backend.tar.gz'],
    ['installer', manifest.installer, 'install.ps1']
  ]) {
    if (
      !metadata ||
      metadata.file !== expectedFile ||
      !Number.isSafeInteger(metadata.size) ||
      metadata.size <= 0 ||
      typeof metadata.sha256 !== 'string' ||
      !SHA256_RE.test(metadata.sha256)
    ) {
      throw new Error(`Payload ${label} metadata is invalid`)
    }
  }
  if (
    !manifest.gitBashRuntime ||
    manifest.gitBashRuntime.file !== WINDOWS_GIT_RUNTIME_FILE ||
    !Number.isSafeInteger(manifest.gitBashRuntime.size) ||
    manifest.gitBashRuntime.size <= 0 ||
    typeof manifest.gitBashRuntime.sha256 !== 'string' ||
    !SHA256_RE.test(manifest.gitBashRuntime.sha256) ||
    !Number.isSafeInteger(manifest.gitBashRuntime.entries) ||
    manifest.gitBashRuntime.entries <= 0 ||
    manifest.gitBashRuntime.source?.file !== WINDOWS_PORTABLE_GIT_RELEASE_FILE ||
    manifest.gitBashRuntime.source?.sha256 !== expectedPortableGitSourceSha256
  ) {
    throw new Error('Payload Git Bash runtime metadata does not match the pinned PortableGit source')
  }
}

function validateInstallStamp(stamp, expectedCommit) {
  if (!stamp || typeof stamp !== 'object' || stamp.schemaVersion !== 1) {
    throw new Error('Install stamp schemaVersion must be 1')
  }
  if (!isRealCommit(stamp.commit)) {
    throw new Error('Install stamp commit must be a real 40-character Git commit')
  }
  if (stamp.dirty !== false) {
    throw new Error('Install stamp must describe a clean build')
  }
  if (expectedCommit && stamp.commit !== expectedCommit) {
    throw new Error(`Install stamp commit ${stamp.commit} does not match expected commit ${expectedCommit}`)
  }
}

function runTar(args, options = {}) {
  const command = process.platform === 'win32' ? 'tar.exe' : 'tar'
  return execFileSync(command, args, {
    encoding: options.encoding || 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'pipe']
  })
}

function auditBackendArchive(archivePath) {
  const entries = runTar(['-tzf', archivePath]).split(/\r?\n/).filter(Boolean)

  if (entries.length === 0) throw new Error('Backend archive is empty')
  for (const entry of entries) {
    assertSafeArchiveEntry(entry)
    if (packagedEntryIsForbidden(entry)) {
      throw new Error(`Backend archive contains forbidden entry: ${entry}`)
    }
  }
  for (const required of REQUIRED_ARCHIVE_ENTRIES) {
    if (!entries.includes(required)) {
      throw new Error(`Backend archive is missing required entry: ${required}`)
    }
  }

  const verboseEntries = runTar(['-tvzf', archivePath]).split(/\r?\n/).filter(Boolean)
  for (const line of verboseEntries) {
    if (line[0] !== '-' && line[0] !== 'd') {
      throw new Error(`Backend archive contains unsupported link or entry type: ${line}`)
    }
  }

  const senseVoiceSource = runTar(['-xOzf', archivePath, 'hermes-agent/tools/sensevoice_stt.py'])
  const modelScopeIndex = senseVoiceSource.indexOf('www.modelscope.cn')
  const githubIndex = senseVoiceSource.indexOf('github.com')
  if (modelScopeIndex < 0 || githubIndex < 0 || modelScopeIndex >= githubIndex) {
    throw new Error('SenseVoice model sources must keep ModelScope first and GitHub second')
  }

  return entries
}

function auditAsar(asarPath) {
  const entries = listPackage(asarPath)
  if (!Array.isArray(entries) || entries.length === 0) throw new Error('app.asar is empty')
  for (const entry of entries) {
    if (packagedEntryIsForbidden(entry)) {
      throw new Error(`app.asar contains forbidden entry: ${entry}`)
    }
  }
  return entries
}

export function auditWindowsPackage({
  resourcesDir,
  expectedCommit = null,
  expectedPortableGitSourceSha256 = WINDOWS_PORTABLE_GIT_RELEASE_SHA256
} = {}) {
  if (!resourcesDir) throw new Error('Windows package audit requires resourcesDir')

  const stampPath = path.join(resourcesDir, 'install-stamp.json')
  const asarPath = path.join(resourcesDir, 'app.asar')
  const bootstrapRoot = path.join(resourcesDir, 'bootstrap')
  const manifestPath = path.join(bootstrapRoot, 'payload-manifest.json')
  const archivePath = path.join(bootstrapRoot, 'hermes-backend.tar.gz')
  const installerPath = path.join(bootstrapRoot, 'install.ps1')
  const gitRuntimePath = path.join(bootstrapRoot, WINDOWS_GIT_RUNTIME_FILE)

  requireRegularFile(stampPath, 'install stamp')
  requireRegularFile(manifestPath, 'payload manifest')
  requireRegularFile(asarPath, 'app.asar')
  const archiveStats = requireRegularFile(archivePath, 'backend archive')
  const installerStats = requireRegularFile(installerPath, 'PowerShell installer')
  const gitRuntimeStats = requireRegularFile(gitRuntimePath, 'Git Bash runtime archive')
  if (fs.existsSync(path.join(bootstrapRoot, 'install.sh'))) {
    throw new Error('Windows bootstrap resources contain the wrong installer: install.sh')
  }

  const stamp = readJson(stampPath, 'install stamp')
  validateInstallStamp(stamp, expectedCommit)
  const manifest = readJson(manifestPath, 'payload manifest')
  validateManifest(manifest, stamp, expectedPortableGitSourceSha256)

  if (archiveStats.size !== manifest.archive.size || sha256File(archivePath) !== manifest.archive.sha256) {
    throw new Error('Backend archive size or SHA-256 does not match payload manifest')
  }
  if (installerStats.size !== manifest.installer.size || sha256File(installerPath) !== manifest.installer.sha256) {
    throw new Error('PowerShell installer size or SHA-256 does not match payload manifest')
  }
  if (
    gitRuntimeStats.size !== manifest.gitBashRuntime.size ||
    sha256File(gitRuntimePath) !== manifest.gitBashRuntime.sha256
  ) {
    throw new Error('Git Bash runtime archive size or SHA-256 does not match payload manifest')
  }

  const archiveEntries = auditBackendArchive(archivePath)
  const gitRuntimeEntries = auditGitRuntimeArchive(gitRuntimePath)
  if (gitRuntimeEntries.length !== manifest.gitBashRuntime.entries) {
    throw new Error('Git Bash runtime entry count does not match payload manifest')
  }
  const asarEntries = auditAsar(asarPath)

  return {
    commit: stamp.commit,
    installerFile: manifest.installer.file,
    archiveEntries: archiveEntries.length,
    gitRuntimeEntries: gitRuntimeEntries.length,
    asarEntries: asarEntries.length
  }
}

export function main(argv = process.argv.slice(2)) {
  let resourcesDir = path.resolve(import.meta.dirname, '..', 'release', 'win-unpacked', 'resources')
  let expectedCommit = process.env.GITHUB_SHA || null
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--resources' && argv[index + 1]) {
      resourcesDir = path.resolve(argv[++index])
    } else if (argv[index] === '--expected-commit' && argv[index + 1]) {
      expectedCommit = argv[++index]
    } else {
      throw new Error(`Unknown package-audit argument: ${argv[index]}`)
    }
  }
  const result = auditWindowsPackage({ resourcesDir, expectedCommit })
  process.stdout.write(
    `[package-audit] Windows bundle ${result.commit.slice(0, 12)} passed ` +
      `(${result.archiveEntries} backend entries, ${result.gitRuntimeEntries} Git runtime entries, ` +
      `${result.asarEntries} ASAR entries)\n`
  )
}

if (isMain(import.meta.url)) {
  try {
    main()
  } catch (error) {
    process.stderr.write(`[package-audit] ${error.message}\n`)
    process.exitCode = 1
  }
}
