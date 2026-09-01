import { execFileSync } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { resolveAnsatzCliPath } from './ansatz-product'
import { AUTH_BRIDGE_PROTOCOL_VERSION, buildAuthBridgeEnvironment } from './auth-bridge'
import { DESKTOP_SCOPE_PROTOCOL_VERSION } from './auth-scope-token'
import { execProbeSync, PROBE_TIMEOUT_MS } from './backend-probes'

const AUTH_MARKER_NAME = '.hermes-auth-bootstrap-complete'
const AUTH_TRANSACTION_NAME = 'auth-venv.pending-backup'
const BUNDLED_SOURCE_MARKER_NAME = '.hermes-bundled-source.json'
const PAYLOAD_MANIFEST_NAME = 'payload-manifest.json'
const SHA256_RE = /^[0-9a-f]{64}$/
const COMMIT_RE = /^[0-9a-f]{40}$/

const AUTH_MARKER_KEYS = [
  'authLockSha256',
  'protocolVersion',
  'schemaVersion',
  'scope',
  'sourceArchiveSha256',
  'sourceCommit'
]

export type AuthRuntimeMarker = {
  schemaVersion: 2
  scope: 'auth'
  sourceCommit: string
  sourceArchiveSha256: string | null
  authLockSha256: string
  protocolVersion: number
}

export type AuthRuntimeContractResult = {
  ok: boolean
  reason: string | null
  pythonPath: string
  marker: AuthRuntimeMarker | null
}

type ResolveGitHead = (activeRoot: string) => string | null

type ProbeOptions = {
  cwd?: string
  env?: NodeJS.ProcessEnv
  stdio: 'ignore'
  timeout: number
  windowsHide?: boolean
}

type ProbeRunner = (command: string, args: string[], options: ProbeOptions) => void

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function hasExactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).sort().join(',') === [...keys].sort().join(',')
}

function readJson(filePath: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch {
    return null
  }
}

function isRegularFile(filePath: string): boolean {
  try {
    return fs.lstatSync(filePath).isFile()
  } catch {
    return false
  }
}

function sha256File(filePath: string): string | null {
  if (!isRegularFile(filePath)) {
    return null
  }

  try {
    return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
  } catch {
    return null
  }
}

function declaredDesktopScopeProtocol(activeRoot: string): number | null {
  const protocolPath = path.join(activeRoot, 'hermes_cli', 'client_auth', 'backend_scope_protocol.py')

  try {
    const source = fs.readFileSync(protocolPath, 'utf8')

    if (source.length > 128 * 1024) {
      return null
    }

    const matches = [
      ...source.matchAll(/^DESKTOP_SCOPE_PROTOCOL_VERSION(?:\s*:\s*[^=\r\n#]+)?\s*=\s*(\d+)(?:\s*#.*)?\s*$/gm)
    ]

    return matches.length === 1 ? Number(matches[0][1]) : null
  } catch {
    return null
  }
}

function parseAuthRuntimeMarker(value: unknown): AuthRuntimeMarker | null {
  if (!isPlainObject(value) || !hasExactKeys(value, AUTH_MARKER_KEYS)) {
    return null
  }

  if (
    value.schemaVersion !== 2 ||
    value.scope !== 'auth' ||
    typeof value.sourceCommit !== 'string' ||
    !COMMIT_RE.test(value.sourceCommit) ||
    (value.sourceArchiveSha256 !== null &&
      (typeof value.sourceArchiveSha256 !== 'string' || !SHA256_RE.test(value.sourceArchiveSha256))) ||
    typeof value.authLockSha256 !== 'string' ||
    !SHA256_RE.test(value.authLockSha256) ||
    value.protocolVersion !== AUTH_BRIDGE_PROTOCOL_VERSION
  ) {
    return null
  }

  return value as AuthRuntimeMarker
}

function readManagedSourceContract(
  activeRoot: string,
  bundledBootstrapRoot: string | null
): { commit: string; archiveSha256: string; source: string | null } | null {

  const sourcePath = path.join(activeRoot, BUNDLED_SOURCE_MARKER_NAME)
  if (!isRegularFile(sourcePath)) {
    return null
  }

  const source = readJson(sourcePath)

  if (
    !isPlainObject(source) ||
    source.schemaVersion !== 1 ||
    typeof source.commit !== 'string' ||
    !COMMIT_RE.test(source.commit) ||
    typeof source.archiveSha256 !== 'string' ||
    !SHA256_RE.test(source.archiveSha256)
  ) {
    return null
  }

  // The active source can be newer than the payload embedded in the shell:
  // `ansatz update` applies a Release Server archive without rebuilding the
  // Electron process.  The source marker is the authoritative contract for
  // that managed runtime.  Keep the payload file check for legacy bundled
  // installs, where the marker has no Release Server provenance.
  const sourceOrigin = typeof source.source === 'string' ? source.source : null

  if (bundledBootstrapRoot && sourceOrigin !== 'release-server') {
    const payloadPath = path.join(bundledBootstrapRoot, PAYLOAD_MANIFEST_NAME)

    if (!isRegularFile(payloadPath)) {
      return null
    }

    const payload = readJson(payloadPath)

    if (
      !isPlainObject(payload) ||
      payload.schemaVersion !== 1 ||
      typeof payload.commit !== 'string' ||
      !COMMIT_RE.test(payload.commit) ||
      !isPlainObject(payload.archive) ||
      typeof payload.archive.sha256 !== 'string' ||
      !SHA256_RE.test(payload.archive.sha256) ||
      source.commit !== payload.commit ||
      source.archiveSha256 !== payload.archive.sha256
    ) {
      return null
    }
  }

  return { commit: source.commit, archiveSha256: source.archiveSha256, source: sourceOrigin }
}

function defaultResolveGitHead(activeRoot: string): string | null {
  try {
    const value = execFileSync('git', ['-c', 'windows.appendAtomically=false', 'rev-parse', 'HEAD'], {
      cwd: activeRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 15_000,
      windowsHide: true
    }).trim()

    return COMMIT_RE.test(value) ? value : null
  } catch {
    return null
  }
}

export function authRuntimeRequiredPaths(
  activeRoot: string,
  platform: NodeJS.Platform = process.platform,
  requireLauncher = true
): string[] {
  const executableDirectory = platform === 'win32' ? 'Scripts' : 'bin'
  const pythonName = platform === 'win32' ? 'python.exe' : 'python'

  const authPython =
    platform === 'win32'
      ? path.join(activeRoot, 'auth-venv', pythonName)
      : path.join(activeRoot, 'auth-venv', executableDirectory, pythonName)

  const required = [
    authPython,
    path.join(activeRoot, 'hermes_cli', 'main.py'),
    path.join(activeRoot, 'hermes_cli', 'client_auth', 'bridge.py'),
    path.join(activeRoot, 'hermes_cli', 'client_auth', 'backend_scope_protocol.py'),
    path.join(activeRoot, 'hermes_cli', 'client_auth', 'cli.py'),
    path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock')
  ]

  if (requireLauncher) {
    const canonical = path.join(activeRoot, 'bin', platform === 'win32' ? 'ansatz.cmd' : 'ansatz')
    const legacy = path.join(activeRoot, 'bin', platform === 'win32' ? 'hermes.cmd' : 'hermes')
    required.push(resolveAnsatzCliPath(canonical, legacy, isRegularFile))
  }

  return required
}

export function validateAuthRuntimeContract({
  activeRoot,
  bundledBootstrapRoot,
  platform = process.platform,
  requireLauncher = true,
  resolveGitHead = defaultResolveGitHead
}: {
  activeRoot: string
  bundledBootstrapRoot: string | null
  platform?: NodeJS.Platform
  requireLauncher?: boolean
  resolveGitHead?: ResolveGitHead
}): AuthRuntimeContractResult {
  const requiredPaths = authRuntimeRequiredPaths(activeRoot, platform, requireLauncher)
  const pythonPath = requiredPaths[0]

  const fail = (reason: string, marker: AuthRuntimeMarker | null = null): AuthRuntimeContractResult => ({
    ok: false,
    reason,
    pythonPath,
    marker
  })

  if (fs.existsSync(path.join(activeRoot, AUTH_TRANSACTION_NAME))) {
    return fail('pending_auth_transaction')
  }

  if (requiredPaths.some(requiredPath => !isRegularFile(requiredPath))) {
    return fail('missing_auth_artifact')
  }

  if (declaredDesktopScopeProtocol(activeRoot) !== DESKTOP_SCOPE_PROTOCOL_VERSION) {
    return fail('scope_protocol_mismatch')
  }

  const markerPath = path.join(activeRoot, AUTH_MARKER_NAME)

  if (!isRegularFile(markerPath)) {
    return fail('invalid_auth_marker')
  }

  const marker = parseAuthRuntimeMarker(readJson(markerPath))

  if (!marker) {
    return fail('invalid_auth_marker')
  }

  const lockHash = sha256File(path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock'))

  if (!lockHash || marker.authLockSha256 !== lockHash) {
    return fail('auth_lock_mismatch', marker)
  }

  const gitMetadata = path.join(activeRoot, '.git')

  if (bundledBootstrapRoot) {
    const managed = readManagedSourceContract(activeRoot, bundledBootstrapRoot)

    if (
      !managed ||
      marker.sourceCommit !== managed.commit ||
      marker.sourceArchiveSha256 !== managed.archiveSha256
    ) {
      return fail('bundled_source_mismatch', marker)
    }
  } else if (fs.existsSync(gitMetadata)) {
    const head = resolveGitHead(activeRoot)

    if (!head || marker.sourceCommit !== head || marker.sourceArchiveSha256 !== null) {
      return fail('git_source_mismatch', marker)
    }
  } else {
    return fail('git_source_mismatch', marker)
  }

  return { ok: true, reason: null, pythonPath, marker }
}

export function authRuntimeProbeSnippet(
  protocolVersion = AUTH_BRIDGE_PROTOCOL_VERSION,
  scopeProtocolVersion = DESKTOP_SCOPE_PROTOCOL_VERSION
): string {
  return [
    'import hermes_cli.client_auth.bridge as bridge',
    'from hermes_cli.client_auth.backend_scope_protocol import DESKTOP_SCOPE_PROTOCOL_VERSION',
    `assert bridge.PROTOCOL_VERSION == ${protocolVersion}`,
    `assert DESKTOP_SCOPE_PROTOCOL_VERSION == ${scopeProtocolVersion}`
  ].join('; ')
}

export function probeAuthRuntime({
  activeRoot,
  pythonPath,
  runProbe = execProbeSync
}: {
  activeRoot: string
  pythonPath: string
  runProbe?: ProbeRunner
}): boolean {
  if (!activeRoot || !pythonPath) {
    return false
  }

  try {
    runProbe(pythonPath, ['-c', authRuntimeProbeSnippet()], {
      cwd: activeRoot,
      env: {
        ...buildAuthBridgeEnvironment(process.env),
        PYTHONPATH: activeRoot
      },
      stdio: 'ignore',
      timeout: PROBE_TIMEOUT_MS,
      windowsHide: true
    })

    return true
  } catch {
    return false
  }
}

export function isAuthRuntimeUsable(options: Parameters<typeof validateAuthRuntimeContract>[0]): boolean {
  const contract = validateAuthRuntimeContract(options)

  return contract.ok && probeAuthRuntime({ activeRoot: options.activeRoot, pythonPath: contract.pythonPath })
}
