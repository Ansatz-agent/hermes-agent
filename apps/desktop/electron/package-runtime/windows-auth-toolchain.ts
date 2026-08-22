import fs from 'node:fs'
import path from 'node:path'

import { buildBootstrapEnvironment, runBootstrapProcess, type BootstrapProcessResult } from '../bootstrap-process'
import type { BundledAuthToolchain } from '../bootstrap-toolchain'

const EXPAND_ARCHIVE_COMMAND = 'Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force'
const PYTHON_PATH_FILE = 'python313._pth'
const PYTHON_PATH_CONTENT = 'python313.zip\n.\nLib\\site-packages\n..\nimport site\n'
const HARD_TIMEOUT_MS = 5 * 60_000
const IDLE_TIMEOUT_MS = 60_000
const KILL_GRACE_MS = 2_000

type ProcessOptions = Parameters<typeof runBootstrapProcess>[0]
type RunProcess = (options: ProcessOptions) => Promise<BootstrapProcessResult>

type PrepareOptions = {
  activeRoot: string
  abortSignal?: AbortSignal
  emit?: ProcessOptions['emit']
  env?: NodeJS.ProcessEnv
  runProcess?: RunProcess
  toolchain: BundledAuthToolchain
}

export function windowsPowerShellExecutable(env: NodeJS.ProcessEnv = process.env): string {
  const systemRoot = env.SystemRoot || env.SYSTEMROOT || env.windir || env.WINDIR || 'C:\\Windows'
  return path.win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

function requireWindowsX64Pe(filePath: string, label: string): void {
  const bytes = fs.readFileSync(filePath)
  if (bytes.length < 0x88 || bytes.toString('ascii', 0, 2) !== 'MZ') {
    throw new Error(`${label} is not PE32+ x64`)
  }
  const peOffset = bytes.readUInt32LE(0x3c)
  if (
    peOffset < 0 ||
    peOffset + 6 > bytes.length ||
    bytes.toString('binary', peOffset, peOffset + 4) !== 'PE\0\0' ||
    bytes.readUInt16LE(peOffset + 4) !== 0x8664
  ) {
    throw new Error(`${label} is not PE32+ x64`)
  }
}

function processFailure(result: BootstrapProcessResult, label: string): Error | null {
  if (result.termination) {
    return new Error(`${label} terminated: ${result.termination}`)
  }
  if (result.code !== 0) {
    const detail = result.stderr.trim() || result.stdout.trim() || `exit ${String(result.code)}`
    return new Error(`${label} failed: ${detail}`)
  }
  return null
}

async function runRequired(runProcess: RunProcess, options: ProcessOptions, label: string): Promise<void> {
  const result = await runProcess(options)
  const failure = processFailure(result, label)
  if (failure) throw failure
}

function assertToolchain(toolchain: BundledAuthToolchain): void {
  if (
    toolchain.manifest.platform !== 'win32' ||
    toolchain.manifest.arch !== 'x64' ||
    toolchain.manifest.python.version !== '3.13.15' ||
    toolchain.manifest.uv.version !== '0.12.5' ||
    path.basename(toolchain.uvAssetPath).toLowerCase() !== 'uv.exe' ||
    path.basename(toolchain.pythonArchivePath).toLowerCase() !== 'python-embed.zip'
  ) {
    throw new Error('Windows authentication toolchain metadata is invalid')
  }
}

function publishDirectory(stagingRuntime: string, runtimeRoot: string): void {
  const backupRoot = `${runtimeRoot}.auth-backup-${process.pid}`
  fs.rmSync(backupRoot, { recursive: true, force: true })
  if (fs.existsSync(runtimeRoot)) fs.renameSync(runtimeRoot, backupRoot)

  try {
    fs.renameSync(stagingRuntime, runtimeRoot)
    fs.rmSync(backupRoot, { recursive: true, force: true })
  } catch (error) {
    if (!fs.existsSync(runtimeRoot) && fs.existsSync(backupRoot)) {
      fs.renameSync(backupRoot, runtimeRoot)
    }
    throw error
  }
}

function publishManagedUv(source: string, activeRoot: string): string {
  const managedBin = path.join(path.dirname(activeRoot), 'bin')
  const destination = path.join(managedBin, 'uv.exe')
  const temporary = `${destination}.tmp-${process.pid}`
  fs.mkdirSync(managedBin, { recursive: true })
  fs.rmSync(temporary, { force: true })
  try {
    fs.copyFileSync(source, temporary)
    requireWindowsX64Pe(temporary, 'managed uv executable')
    fs.rmSync(destination, { force: true })
    fs.renameSync(temporary, destination)
    return destination
  } finally {
    fs.rmSync(temporary, { force: true })
  }
}

export async function prepareWindowsPackagedAuthRuntime(options: PrepareOptions): Promise<{
  managedUvPath: string
  pythonExecutable: string
  runtimeRoot: string
}> {
  const {
    activeRoot,
    abortSignal,
    emit,
    env = process.env,
    runProcess = runBootstrapProcess,
    toolchain
  } = options

  if (!path.isAbsolute(activeRoot)) throw new Error('Windows authentication runtime root must be absolute')
  assertToolchain(toolchain)
  fs.mkdirSync(activeRoot, { recursive: true })

  const stagingRoot = path.join(activeRoot, `.auth-runtime-stage-${process.pid}-${Date.now()}`)
  const stagingRuntime = path.join(stagingRoot, 'venv')
  const runtimeRoot = path.join(activeRoot, 'venv')
  const pythonExecutable = path.join(stagingRuntime, 'python.exe')
  const uvExecutable = path.join(stagingRuntime, 'uv.exe')
  const safeEnv = buildBootstrapEnvironment(env, { hermesHome: path.dirname(activeRoot) })
  safeEnv.PIP_CONFIG_FILE = 'NUL'
  safeEnv.UV_NO_CONFIG = '1'

  fs.rmSync(stagingRoot, { recursive: true, force: true })
  fs.mkdirSync(stagingRuntime, { recursive: true })

  try {
    await runRequired(
      runProcess,
      {
        command: windowsPowerShellExecutable(env),
        args: [
          '-NoProfile',
          '-NonInteractive',
          '-ExecutionPolicy',
          'Bypass',
          '-Command',
          EXPAND_ARCHIVE_COMMAND,
          toolchain.pythonArchivePath,
          stagingRuntime
        ],
        abortSignal,
        emit,
        env: safeEnv,
        hardTimeoutMs: HARD_TIMEOUT_MS,
        idleTimeoutMs: IDLE_TIMEOUT_MS,
        killGraceMs: KILL_GRACE_MS,
        stageName: 'auth-prerequisites'
      },
      'Windows Python archive extraction'
    )

    requireWindowsX64Pe(pythonExecutable, 'Python executable')
    requireWindowsX64Pe(toolchain.uvAssetPath, 'uv executable')
    fs.copyFileSync(toolchain.uvAssetPath, uvExecutable)
    fs.mkdirSync(path.join(stagingRuntime, 'Lib', 'site-packages'), { recursive: true })
    fs.writeFileSync(path.join(stagingRuntime, PYTHON_PATH_FILE), PYTHON_PATH_CONTENT, 'utf8')

    await runRequired(
      runProcess,
      {
        command: uvExecutable,
        args: [
          'pip',
          'install',
          '--python',
          pythonExecutable,
          '--no-index',
          '--find-links',
          path.join(toolchain.root, 'wheelhouse'),
          '--require-hashes',
          '--requirement',
          toolchain.requirementsPath,
          '--disable-pip-version-check',
          '--no-progress'
        ],
        abortSignal,
        cwd: activeRoot,
        emit,
        env: safeEnv,
        hardTimeoutMs: HARD_TIMEOUT_MS,
        idleTimeoutMs: IDLE_TIMEOUT_MS,
        killGraceMs: KILL_GRACE_MS,
        stageName: 'python-auth-deps'
      },
      'offline authentication dependency installation'
    )

    const verification = [
      'import httpx, keyring',
      'from hermes_cli.client_auth import bridge',
      'backend = keyring.get_keyring()',
      "identity = f'{backend.__class__.__module__}.{backend.__class__.__name__}'",
      "assert 'Windows' in identity, identity"
    ].join('; ')
    await runRequired(
      runProcess,
      {
        command: pythonExecutable,
        args: ['-I', '-c', verification],
        abortSignal,
        cwd: activeRoot,
        emit,
        env: safeEnv,
        hardTimeoutMs: 30_000,
        idleTimeoutMs: 20_000,
        killGraceMs: KILL_GRACE_MS,
        stageName: 'auth-complete'
      },
      'Windows authentication runtime verification'
    )

    const managedUvPath = publishManagedUv(uvExecutable, activeRoot)
    publishDirectory(stagingRuntime, runtimeRoot)
    return { managedUvPath, runtimeRoot, pythonExecutable: path.join(runtimeRoot, 'python.exe') }
  } finally {
    fs.rmSync(stagingRoot, { recursive: true, force: true })
  }
}
