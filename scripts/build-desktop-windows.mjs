import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import {
  assertX64HermesExecutable,
  findNewestWindowsNsis,
  forbiddenBrowserDownloadLine
} from './desktop-windows-contract.mjs'

const REPO_ROOT = path.resolve(import.meta.dirname, '..')
const BUILD_LOG_NAME = 'phase1-desktop-windows-nsis-build.log'
const POINTER_NAME = 'phase1-desktop-windows-nsis-artifact.txt'

export function validateWindowsBuildHost({ platform, arch, actualNodeVersion, expectedNodeVersion }) {
  if (platform !== 'win32' || arch !== 'x64') {
    throw new Error(`Windows x64 is required; got ${platform}-${arch}`)
  }
  if (actualNodeVersion !== expectedNodeVersion) {
    throw new Error(`expected ${expectedNodeVersion}, got ${actualNodeVersion}`)
  }
}

export function buildWindowsEnvironment(baseEnv = {}) {
  return {
    ...baseEnv,
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1',
    ELECTRON_MIRROR: baseEnv.ELECTRON_MIRROR || 'https://npmmirror.com/mirrors/electron/',
    ELECTRON_BUILDER_BINARIES_MIRROR:
      baseEnv.ELECTRON_BUILDER_BINARIES_MIRROR || 'https://npmmirror.com/mirrors/electron-builder-binaries/',
    CSC_IDENTITY_AUTO_DISCOVERY: 'false'
  }
}

export function resolveNpmInvocation({ platform, nodeExecutable, npmExecPath }) {
  if (platform === 'win32') {
    if (!npmExecPath) {
      throw new Error('npm_execpath is required on Windows; run this build through npm')
    }
    return { command: nodeExecutable, argsPrefix: [npmExecPath] }
  }
  return { command: 'npm', argsPrefix: [] }
}

export function windowsBuildCommands(npmInvocation = { command: 'npm', argsPrefix: [] }) {
  const { command, argsPrefix } = npmInvocation
  return [
    { command, args: [...argsPrefix, 'ci'], env: {} },
    {
      command,
      args: [...argsPrefix, 'run', '--workspace', 'apps/desktop', 'dist:win:nsis'],
      env: {}
    },
    {
      command,
      args: [...argsPrefix, 'run', '--workspace', 'apps/desktop', 'test:desktop:nsis'],
      env: { HERMES_DESKTOP_SKIP_BUILD: '1' }
    }
  ]
}

export function runLogged({ command, args, cwd, env, logPath }) {
  fs.appendFileSync(logPath, `\n>>> ${command} ${args.join(' ')}\n`, 'utf8')
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    const copy = (target, chunk) => {
      target.write(chunk)
      fs.appendFileSync(logPath, chunk)
    }
    child.stdout.on('data', chunk => copy(process.stdout, chunk))
    child.stderr.on('data', chunk => copy(process.stderr, chunk))
    child.on('error', reject)
    child.on('close', code => {
      if (code === 0) resolve()
      else reject(new Error(`${command} ${args.join(' ')} failed with exit code ${code}`))
    })
  })
}

export async function runWindowsBuild(options = {}) {
  const repoRoot = options.repoRoot || REPO_ROOT
  const releaseDir = options.releaseDir || path.join(repoRoot, 'apps', 'desktop', 'release')
  const logDir = options.logDir || path.join(repoRoot, 'apps', 'desktop', 'build', 'logs')
  const buildLog = options.buildLog || path.join(logDir, BUILD_LOG_NAME)
  const pointer = options.artifactPointer || path.join(logDir, POINTER_NAME)
  const expectedNodeVersion = `v${fs.readFileSync(path.join(repoRoot, '.node-version'), 'utf8').trim()}`
  const platform = options.platform || process.platform
  const arch = options.arch || process.arch
  const actualNodeVersion = options.actualNodeVersion || process.version
  const now = options.now || Date.now
  const runner = options.runner || runLogged

  validateWindowsBuildHost({ platform, arch, actualNodeVersion, expectedNodeVersion })
  fs.mkdirSync(releaseDir, { recursive: true })
  fs.mkdirSync(logDir, { recursive: true })
  fs.writeFileSync(buildLog, '', 'utf8')
  const startedAt = now()
  const env = buildWindowsEnvironment(options.env || process.env)
  const npmInvocation =
    options.npmInvocation ||
    resolveNpmInvocation({
      platform,
      nodeExecutable: options.nodeExecutable || process.execPath,
      npmExecPath: options.npmExecPath || env.npm_execpath
    })

  for (const step of windowsBuildCommands(npmInvocation)) {
    await runner({
      command: step.command,
      args: step.args,
      cwd: repoRoot,
      env: { ...env, ...step.env },
      logPath: buildLog
    })
  }

  const forbidden = forbiddenBrowserDownloadLine(fs.readFileSync(buildLog, 'utf8'))
  if (forbidden) throw new Error(`Playwright browser download detected: ${forbidden}`)
  assertX64HermesExecutable(releaseDir)
  const installer = findNewestWindowsNsis(releaseDir, startedAt)
  fs.writeFileSync(pointer, `${installer}\n`, 'utf8')
  return { installer, buildLog, artifactPointer: pointer }
}

async function main(argv) {
  const expectedNodeVersion = `v${fs.readFileSync(path.join(REPO_ROOT, '.node-version'), 'utf8').trim()}`
  validateWindowsBuildHost({
    platform: process.platform,
    arch: process.arch,
    actualNodeVersion: process.version,
    expectedNodeVersion
  })
  if (argv.length === 1 && argv[0] === '--check') {
    const env = buildWindowsEnvironment(process.env)
    process.stdout.write(
      `Node=${process.version}\nPLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=${env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD}\n`
    )
    return
  }
  if (argv.length !== 0) throw new Error(`unknown argument: ${argv[0]}`)
  const result = await runWindowsBuild()
  process.stdout.write(`Ansatz Voice Trace Client Windows NSIS ready: ${result.installer}\nBuild log: ${result.buildLog}\n`)
}

const invoked = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : ''
if (invoked === import.meta.url) {
  main(process.argv.slice(2)).catch(error => {
    process.stderr.write(`Ansatz Voice Trace Client Windows NSIS build: ${error.message}\n`)
    process.exitCode = 1
  })
}
