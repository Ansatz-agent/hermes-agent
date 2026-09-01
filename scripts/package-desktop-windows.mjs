/**
 * Build and verify the Ansatz Windows x64 NSIS installer.
 *
 * This is the single local entry point for the same sequence used by the
 * Windows packaging workflow: locked dependency installation, product
 * contracts, NSIS packaging, payload audit, and a clean-install smoke test.
 * The script deliberately delegates packaging and auditing to the repository's
 * existing tools so local builds and CI cannot silently drift apart.
 */

import { execFileSync, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { assertX64HermesExecutable, findNewestWindowsNsis } from './desktop-windows-contract.mjs'
import {
  WINDOWS_PORTABLE_GIT_RELEASE_FILE,
  WINDOWS_PORTABLE_GIT_RELEASE_SHA256
} from '../apps/desktop/scripts/prepare-windows-git-runtime.mjs'
import { auditWindowsPackage } from '../apps/desktop/scripts/package-audit.mjs'

const REPO_ROOT = path.resolve(import.meta.dirname, '..')
const DESKTOP_ROOT = path.join(REPO_ROOT, 'apps', 'desktop')
const RELEASE_DIR = path.join(DESKTOP_ROOT, 'release')
const LOG_DIR = path.join(DESKTOP_ROOT, 'build', 'logs')
const REPORT_DIR = path.join(DESKTOP_ROOT, 'build', 'reports')
const BUILD_LOG = path.join(LOG_DIR, 'desktop-windows-package.log')
const REPORT_PATH = path.join(REPORT_DIR, 'windows-package.json')
const EXPECTED_NODE_VERSION = `v${fs.readFileSync(path.join(REPO_ROOT, '.node-version'), 'utf8').trim()}`
const PORTABLE_GIT_CACHE_DIR = path.join(DESKTOP_ROOT, 'build', 'package-input-cache')
const PORTABLE_GIT_CACHE_PATH = path.join(PORTABLE_GIT_CACHE_DIR, WINDOWS_PORTABLE_GIT_RELEASE_FILE)
const PORTABLE_GIT_URL = `https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.3/${WINDOWS_PORTABLE_GIT_RELEASE_FILE}`

export const WINDOWS_PACKAGE_ENVIRONMENT = Object.freeze({
  NPM_CONFIG_REGISTRY: 'https://registry.npmmirror.com',
  NPM_CONFIG_REPLACE_REGISTRY_HOST: 'always',
  UV_DEFAULT_INDEX: 'https://mirrors.ustc.edu.cn/pypi/simple',
  HERMES_UV_FALLBACK_INDEX: 'https://pypi.tuna.tsinghua.edu.cn/simple',
  ELECTRON_MIRROR: 'https://npmmirror.com/mirrors/electron/',
  ELECTRON_BUILDER_BINARIES_MIRROR: 'https://npmmirror.com/mirrors/electron-builder-binaries/',
  PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1',
  CSC_IDENTITY_AUTO_DISCOVERY: 'false'
})

export function validateWindowsPackageHost({ platform, arch, nodeVersion }) {
  if (platform !== 'win32' || arch !== 'x64') {
    throw new Error(`Ansatz Windows packaging requires win32-x64; got ${platform}-${arch}`)
  }
  if (nodeVersion !== EXPECTED_NODE_VERSION) {
    throw new Error(`Ansatz Windows packaging requires Node ${EXPECTED_NODE_VERSION}; got ${nodeVersion}`)
  }
}

export function packageWindowsCommands(npmCommand = 'npm.cmd') {
  return [
    { command: npmCommand, args: ['ci'], name: 'install locked dependencies' },
    {
      command: npmCommand,
      args: ['run', 'test:desktop:windows-contract'],
      name: 'run Windows product contracts'
    },
    {
      command: npmCommand,
      args: ['run', 'typecheck', '--workspace', 'apps/desktop'],
      name: 'typecheck desktop'
    },
    {
      command: npmCommand,
      args: ['run', 'dist:win:nsis', '--workspace', 'apps/desktop'],
      name: 'build NSIS installer'
    },
    {
      command: npmCommand,
      args: ['run', 'test:desktop:nsis', '--workspace', 'apps/desktop'],
      name: 'validate unpacked desktop bundle'
    }
  ]
}

function sha256File(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function assertPortableGitSource(sourcePath) {
  if (!fs.existsSync(sourcePath)) throw new Error(`PortableGit source archive is missing: ${sourcePath}`)
  const actual = sha256File(sourcePath)
  if (actual !== WINDOWS_PORTABLE_GIT_RELEASE_SHA256) {
    throw new Error(
      `PortableGit source SHA-256 mismatch for ${sourcePath}: ` +
        `expected ${WINDOWS_PORTABLE_GIT_RELEASE_SHA256}, got ${actual}`
    )
  }
  return sourcePath
}

export async function ensurePortableGitSource({ sourcePath = PORTABLE_GIT_CACHE_PATH, fetchImpl = fetch } = {}) {
  const resolvedPath = path.resolve(sourcePath)
  if (fs.existsSync(resolvedPath)) return assertPortableGitSource(resolvedPath)

  if (resolvedPath !== PORTABLE_GIT_CACHE_PATH) {
    throw new Error(`PortableGit source archive is missing: ${resolvedPath}`)
  }

  fs.mkdirSync(PORTABLE_GIT_CACHE_DIR, { recursive: true })
  const temporaryPath = `${resolvedPath}.download-${process.pid}-${Date.now()}`
  try {
    const response = await fetchImpl(PORTABLE_GIT_URL, { signal: AbortSignal.timeout(120_000) })
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`)
    const bytes = Buffer.from(await response.arrayBuffer())
    fs.writeFileSync(temporaryPath, bytes)
    assertPortableGitSource(temporaryPath)
    fs.renameSync(temporaryPath, resolvedPath)
    return resolvedPath
  } catch (error) {
    throw new Error(`Unable to prepare verified PortableGit input: ${error.message}`)
  } finally {
    fs.rmSync(temporaryPath, { force: true })
  }
}

function readDesktopPackage() {
  return JSON.parse(fs.readFileSync(path.join(DESKTOP_ROOT, 'package.json'), 'utf8'))
}

function readPackageVersion() {
  return readDesktopPackage().version
}

function resolveNpmCommand(env = process.env) {
  // npm.cmd is the stable executable on Windows. When invoked through npm,
  // npm_execpath is preferred so the same pinned npm installation is reused.
  if (process.platform !== 'win32') return 'npm'
  return env.npm_execpath ? process.execPath : 'npm.cmd'
}

function resolveNpmArgs(env = process.env) {
  return process.platform === 'win32' && env.npm_execpath ? [env.npm_execpath] : []
}

function runStep({ command, args, env, name }) {
  const line = `\n>>> ${name}: ${command} ${args.join(' ')}\n`
  fs.appendFileSync(BUILD_LOG, line, 'utf8')
  process.stdout.write(line)
  const result = spawnSync(command, args, {
    cwd: REPO_ROOT,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
    windowsHide: true,
    maxBuffer: 64 * 1024 * 1024
  })
  const stdout = result.stdout || ''
  const stderr = result.stderr || ''
  fs.appendFileSync(BUILD_LOG, stdout + stderr, 'utf8')
  process.stdout.write(stdout)
  process.stderr.write(stderr)
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with exit code ${result.status}`)
  }
}

function runPowerShellSmoke(installer, expectedCommit, packageJson, env) {
  const smokeScript = path.join(REPO_ROOT, 'scripts', 'test-desktop-windows-install.ps1')
  const smokeLog = path.join(LOG_DIR, 'desktop-windows-install-smoke.log')
  const args = [
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    smokeScript,
    '-InstallerPath',
    installer,
    '-ExpectedCommit',
    expectedCommit,
    '-ExpectedVersion',
    packageJson.version,
    '-ProductName',
    packageJson.productName,
    '-ExecutableName',
    packageJson.build.executableName,
    '-LogPath',
    smokeLog
  ]
  runStep({
    command: 'pwsh',
    args,
    env,
    name: 'run clean-install smoke test'
  })
  return smokeLog
}

function writeReport({ installer, commit, audit, smokeLog }) {
  const report = {
    schemaVersion: 1,
    commit,
    version: readPackageVersion(),
    platform: 'windows',
    arch: 'x64',
    artifact: path.basename(installer),
    bytes: fs.statSync(installer).size,
    sha256: sha256File(installer),
    audit,
    smokeLog: path.relative(REPO_ROOT, smokeLog).replaceAll(path.sep, '/'),
    sensitiveEvidenceRecorded: false
  }
  fs.writeFileSync(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  return report
}

export async function packageWindows(options = {}) {
  const env = { ...process.env, ...WINDOWS_PACKAGE_ENVIRONMENT, ...(options.env || {}) }
  validateWindowsPackageHost({
    platform: options.platform || process.platform,
    arch: options.arch || process.arch,
    nodeVersion: options.nodeVersion || process.version
  })

  const commit =
    options.commit ||
    execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: REPO_ROOT,
      encoding: 'utf8'
    }).trim()
  if (!/^[0-9a-f]{40}$/i.test(commit)) throw new Error(`Invalid source commit: ${commit}`)

  fs.mkdirSync(LOG_DIR, { recursive: true })
  fs.mkdirSync(REPORT_DIR, { recursive: true })
  fs.writeFileSync(BUILD_LOG, '', 'utf8')
  const startedAtMs = Date.now()

  const portableGitSource = await ensurePortableGitSource({
    sourcePath: options.portableGitSource || env.HERMES_WINDOWS_PORTABLE_GIT_SOURCE || PORTABLE_GIT_CACHE_PATH
  })
  env.HERMES_WINDOWS_PORTABLE_GIT_SOURCE = portableGitSource
  const inputLine =
    `[inputs] verified PortableGit: ${portableGitSource} ` + `(sha256 ${WINDOWS_PORTABLE_GIT_RELEASE_SHA256})\n`
  fs.appendFileSync(BUILD_LOG, inputLine, 'utf8')
  process.stdout.write(inputLine)

  const npmCommand = options.npmCommand || resolveNpmCommand(env)
  const npmPrefix = options.npmPrefix || resolveNpmArgs(env)
  const commandPlan = packageWindowsCommands(npmCommand).map(step => ({
    ...step,
    args: [...npmPrefix, ...step.args]
  }))

  for (const step of commandPlan) runStep({ ...step, env })

  const releaseDir = options.releaseDir || RELEASE_DIR
  const installer = findNewestWindowsNsis(releaseDir, startedAtMs)
  assertX64HermesExecutable(releaseDir)

  const audit = auditWindowsPackage({
    resourcesDir: path.join(releaseDir, 'win-unpacked', 'resources'),
    expectedCommit: commit,
    expectedPortableGitSourceSha256: WINDOWS_PORTABLE_GIT_RELEASE_SHA256
  })

  const smokeLog = options.skipInstallSmoke
    ? path.join(LOG_DIR, 'desktop-windows-install-smoke.skipped.log')
    : runPowerShellSmoke(installer, commit, readDesktopPackage(), env)
  if (options.skipInstallSmoke) {
    fs.writeFileSync(smokeLog, 'Install smoke test skipped by --skip-install-smoke.\n', 'utf8')
  }

  const report = writeReport({ installer, commit, audit, smokeLog })
  return { installer, buildLog: BUILD_LOG, reportPath: REPORT_PATH, report }
}

export function main(argv = process.argv.slice(2)) {
  const options = {}
  for (const arg of argv) {
    if (arg === '--skip-install-smoke') options.skipInstallSmoke = true
    else if (arg === '--check') {
      validateWindowsPackageHost({ platform: process.platform, arch: process.arch, nodeVersion: process.version })
      process.stdout.write(`Node=${process.version}\nPlatform=${process.platform}-${process.arch}\n`)
      return Promise.resolve()
    } else throw new Error(`Unknown argument: ${arg}`)
  }

  return packageWindows(options).then(result => {
    process.stdout.write(`\nAnsatz Windows package ready: ${result.installer}\n`)
    process.stdout.write(`Build log: ${result.buildLog}\nReport: ${result.reportPath}\n`)
  })
}

const invoked = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : ''
if (invoked === import.meta.url) {
  main().catch(error => {
    process.stderr.write(`Ansatz Windows package failed: ${error.message}\n`)
    process.exitCode = 1
  })
}
