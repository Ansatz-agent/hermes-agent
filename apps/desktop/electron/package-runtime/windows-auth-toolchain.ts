import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { AUTH_BRIDGE_PROTOCOL_VERSION } from '../auth-bridge'
import { DESKTOP_SCOPE_PROTOCOL_VERSION } from '../auth-scope-token'
import { type BootstrapProcessResult, buildBootstrapEnvironment, runBootstrapProcess } from '../bootstrap-process'
import type { BundledAuthToolchain } from '../bootstrap-toolchain'
import { retireExactWindowsAuthOwners } from '../windows-auth-owner'

const EXPAND_ARCHIVE_COMMAND =
  'Expand-Archive -LiteralPath $env:HERMES_ARCHIVE_PATH -DestinationPath $env:HERMES_ARCHIVE_DESTINATION -Force'

const PYTHON_PATH_FILE = 'python313._pth'
const PYTHON_PATH_CONTENT = 'python313.zip\n.\nLib\\site-packages\n..\nimport site\n'
const HARD_TIMEOUT_MS = 5 * 60_000
const IDLE_TIMEOUT_MS = 60_000
const KILL_GRACE_MS = 2_000
const AUTH_MARKER_NAME = '.hermes-auth-bootstrap-complete'
const AUTH_TRANSACTION_NAME = 'auth-venv.pending-backup'

type ProcessOptions = Parameters<typeof runBootstrapProcess>[0]
type RunProcess = (options: ProcessOptions) => Promise<BootstrapProcessResult>
type RetireAuthOwners = typeof retireExactWindowsAuthOwners

type PrepareOptions = {
  activeRoot: string
  abortSignal?: AbortSignal
  emit?: ProcessOptions['emit']
  env?: NodeJS.ProcessEnv
  runProcess?: RunProcess
  retireAuthOwners?: RetireAuthOwners
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

  if (failure) {
    throw failure
  }
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

type AuthTransaction = {
  backupName: string | null
  markerBase64: string | null
  markerExisted: boolean
  schemaVersion: 1
}

function authTransactionPath(activeRoot: string): string {
  return path.join(activeRoot, AUTH_TRANSACTION_NAME)
}

function readAuthTransaction(activeRoot: string): AuthTransaction | null {
  const transactionPath = authTransactionPath(activeRoot)

  if (!fs.existsSync(transactionPath)) {
    return null
  }

  let value: unknown

  try {
    const raw = fs.readFileSync(transactionPath)

    if (raw.length > 131_072) {
      throw new Error('transaction record is too large')
    }

    value = JSON.parse(raw.toString('utf8'))
  } catch {
    throw new Error('Windows authentication environment transaction is invalid')
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Windows authentication environment transaction is invalid')
  }

  const record = value as Record<string, unknown>
  const keys = Object.keys(record).sort().join(',')

  if (
    keys !== 'backupName,markerBase64,markerExisted,schemaVersion' ||
    record.schemaVersion !== 1 ||
    typeof record.markerExisted !== 'boolean' ||
    (record.backupName !== null &&
      (typeof record.backupName !== 'string' || !/^auth-venv\.stale-\d+-\d+$/.test(record.backupName))) ||
    (record.markerExisted && typeof record.markerBase64 !== 'string') ||
    (!record.markerExisted && record.markerBase64 !== null)
  ) {
    throw new Error('Windows authentication environment transaction is invalid')
  }

  if (record.markerExisted && Buffer.from(record.markerBase64 as string, 'base64').length > 65_536) {
    throw new Error('Windows authentication environment transaction is invalid')
  }

  return record as AuthTransaction
}

export function recoverWindowsAuthRuntimeTransaction(activeRoot: string): void {
  const transaction = readAuthTransaction(activeRoot)

  if (!transaction) {
    return
  }

  const runtimeRoot = path.join(activeRoot, 'auth-venv')
  const backupRoot = transaction.backupName ? path.join(activeRoot, transaction.backupName) : null
  const failedRoot = path.join(activeRoot, `auth-venv.failed-${process.pid}-${Date.now()}`)

  if (backupRoot && fs.existsSync(backupRoot)) {
    if (fs.existsSync(runtimeRoot)) {
      fs.renameSync(runtimeRoot, failedRoot)
    }

    fs.renameSync(backupRoot, runtimeRoot)
  } else if (backupRoot && !fs.existsSync(runtimeRoot)) {
    throw new Error('Windows authentication environment backup is missing')
  } else if (!backupRoot && fs.existsSync(runtimeRoot)) {
    fs.renameSync(runtimeRoot, failedRoot)
  }

  const markerPath = path.join(activeRoot, AUTH_MARKER_NAME)

  if (transaction.markerExisted) {
    writeAtomic(markerPath, Buffer.from(transaction.markerBase64 as string, 'base64').toString('utf8'))
  } else {
    fs.rmSync(markerPath, { force: true })
  }

  fs.rmSync(authTransactionPath(activeRoot), { force: true })
  fs.rmSync(failedRoot, { recursive: true, force: true })
}

function beginAuthTransaction(activeRoot: string): AuthTransaction {
  const runtimeRoot = path.join(activeRoot, 'auth-venv')
  const markerPath = path.join(activeRoot, AUTH_MARKER_NAME)
  const markerExisted = fs.existsSync(markerPath)
  const marker = markerExisted ? fs.readFileSync(markerPath) : null

  if (marker && marker.length > 65_536) {
    throw new Error('Previous Windows authentication marker is too large')
  }

  const transaction: AuthTransaction = {
    schemaVersion: 1,
    backupName: fs.existsSync(runtimeRoot) ? `auth-venv.stale-${process.pid}-${Date.now()}` : null,
    markerExisted,
    markerBase64: marker ? marker.toString('base64') : null
  }

  writeAtomic(authTransactionPath(activeRoot), `${JSON.stringify(transaction)}\n`)

  return transaction
}

function publishAuthRuntime(stagingRuntime: string, activeRoot: string, transaction: AuthTransaction): void {
  const runtimeRoot = path.join(activeRoot, 'auth-venv')

  if (transaction.backupName) {
    fs.renameSync(runtimeRoot, path.join(activeRoot, transaction.backupName))
  }

  fs.renameSync(stagingRuntime, runtimeRoot)
}

function completeAuthTransaction(activeRoot: string, transaction: AuthTransaction): void {
  fs.rmSync(authTransactionPath(activeRoot), { force: true })

  if (transaction.backupName) {
    fs.rmSync(path.join(activeRoot, transaction.backupName), { recursive: true, force: true })
  }
}

function sha256File(filePath: string): string {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function readSourceContract(activeRoot: string): { commit: string; archiveSha256: string } {
  const sourcePath = path.join(activeRoot, '.hermes-bundled-source.json')
  const lockPath = path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock')
  let source: unknown

  try {
    source = JSON.parse(fs.readFileSync(sourcePath, 'utf8'))
  } catch {
    throw new Error('Windows authentication source contract is missing or invalid')
  }

  if (
    !source ||
    typeof source !== 'object' ||
    Array.isArray(source) ||
    (source as Record<string, unknown>).schemaVersion !== 1 ||
    typeof (source as Record<string, unknown>).commit !== 'string' ||
    !/^[0-9a-f]{40}$/.test((source as Record<string, unknown>).commit as string) ||
    typeof (source as Record<string, unknown>).archiveSha256 !== 'string' ||
    !/^[0-9a-f]{64}$/.test((source as Record<string, unknown>).archiveSha256 as string) ||
    !fs.statSync(lockPath, { throwIfNoEntry: false })?.isFile()
  ) {
    throw new Error('Windows authentication source contract is missing or invalid')
  }

  return {
    commit: (source as Record<string, string>).commit,
    archiveSha256: (source as Record<string, string>).archiveSha256
  }
}

function writeAtomic(filePath: string, contents: string): void {
  const temporary = `${filePath}.tmp-${process.pid}-${Date.now()}`
  const backup = `${filePath}.replace-${process.pid}-${Date.now()}`
  fs.mkdirSync(path.dirname(filePath), { recursive: true })

  try {
    fs.writeFileSync(temporary, contents, { encoding: 'utf8', flag: 'wx', mode: 0o600 })

    if (fs.existsSync(filePath)) {
      fs.renameSync(filePath, backup)

      try {
        fs.renameSync(temporary, filePath)
        fs.rmSync(backup, { force: true })
      } catch (error) {
        if (!fs.existsSync(filePath) && fs.existsSync(backup)) {
          fs.renameSync(backup, filePath)
        }

        throw error
      }
    } else {
      fs.renameSync(temporary, filePath)
    }
  } finally {
    fs.rmSync(temporary, { force: true })
    fs.rmSync(backup, { force: true })
  }
}

function publishAuthContract(activeRoot: string, source: { commit: string; archiveSha256: string }): void {
  const lockPath = path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock')

  const launcher = [
    '@echo off',
    'set "PYTHONPATH=%~dp0.."',
    '"%~dp0..\\auth-venv\\python.exe" -m hermes_cli.main %*',
    ''
  ].join('\r\n')

  const legacyLauncher = ['@echo off', 'call "%~dp0ansatz.cmd" %*', ''].join('\r\n')

  writeAtomic(path.join(activeRoot, 'bin', 'ansatz.cmd'), launcher)
  writeAtomic(path.join(activeRoot, 'bin', 'hermes.cmd'), legacyLauncher)
  writeAtomic(
    path.join(activeRoot, '.hermes-auth-bootstrap-complete'),
    `${JSON.stringify({
      schemaVersion: 2,
      scope: 'auth',
      sourceCommit: source.commit,
      sourceArchiveSha256: source.archiveSha256,
      authLockSha256: sha256File(lockPath),
      protocolVersion: AUTH_BRIDGE_PROTOCOL_VERSION
    })}\n`
  )
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
    retireAuthOwners = process.platform === 'win32'
      ? retireExactWindowsAuthOwners
      : async () => ({ inspected: 0, stopped: 0 }),
    toolchain
  } = options

  if (!path.isAbsolute(activeRoot)) {
    throw new Error('Windows authentication runtime root must be absolute')
  }

  assertToolchain(toolchain)
  fs.mkdirSync(activeRoot, { recursive: true })
  const sourceContract = readSourceContract(activeRoot)
  // A previous build may have launched its owner from either the packaged
  // embedded interpreter or the standard venv Scripts layout.  Both are
  // exact Ansatz-owned paths; retire them before replacing auth-venv so an
  // old owner cannot reconnect to the newly published bridge contract.
  await retireAuthOwners({ activeRoot, includeLegacyVenv: true })
  recoverWindowsAuthRuntimeTransaction(activeRoot)

  const stagingRoot = path.join(activeRoot, `.auth-runtime-stage-${process.pid}-${Date.now()}`)
  const stagingRuntime = path.join(stagingRoot, 'venv')
  const runtimeRoot = path.join(activeRoot, 'auth-venv')
  const pythonExecutable = path.join(stagingRuntime, 'python.exe')
  const uvExecutable = path.join(stagingRuntime, 'uv.exe')
  const safeEnv = buildBootstrapEnvironment(env, { hermesHome: path.dirname(activeRoot) })
  safeEnv.PIP_CONFIG_FILE = 'NUL'
  safeEnv.UV_NO_CONFIG = '1'

  fs.rmSync(stagingRoot, { recursive: true, force: true })
  fs.mkdirSync(stagingRuntime, { recursive: true })

  let transaction: AuthTransaction | null = null

  try {
    await runRequired(
      runProcess,
      {
        command: windowsPowerShellExecutable(env),
        args: ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', EXPAND_ARCHIVE_COMMAND],
        abortSignal,
        emit,
        env: {
          ...safeEnv,
          HERMES_ARCHIVE_PATH: toolchain.pythonArchivePath,
          HERMES_ARCHIVE_DESTINATION: stagingRuntime
        },
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
      'from hermes_cli.client_auth.backend_scope_protocol import DESKTOP_SCOPE_PROTOCOL_VERSION',
      `assert bridge.PROTOCOL_VERSION == ${AUTH_BRIDGE_PROTOCOL_VERSION}`,
      `assert DESKTOP_SCOPE_PROTOCOL_VERSION == ${DESKTOP_SCOPE_PROTOCOL_VERSION}`,
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
    transaction = beginAuthTransaction(activeRoot)
    publishAuthRuntime(stagingRuntime, activeRoot, transaction)
    publishAuthContract(activeRoot, sourceContract)
    completeAuthTransaction(activeRoot, transaction)
    transaction = null

    return { managedUvPath, runtimeRoot, pythonExecutable: path.join(runtimeRoot, 'python.exe') }
  } catch (error) {
    if (transaction) {
      recoverWindowsAuthRuntimeTransaction(activeRoot)
    }

    throw error
  } finally {
    fs.rmSync(stagingRoot, { recursive: true, force: true })
  }
}
