import { spawn } from 'node:child_process'

import type { BridgeStatus } from '../auth-bridge-status'

export type { BridgeStatus } from '../auth-bridge-status'

export const AUTH_BRIDGE_PROTOCOL_VERSION = 2
const PROTOCOL_VERSION = AUTH_BRIDGE_PROTOCOL_VERSION
const MAX_LINE_BYTES = 64 * 1024
const MAX_REQUEST_ID_LENGTH = 64
const DEFAULT_TIMEOUT_MS = 20_000
const DEFAULT_LOGIN_TIMEOUT_MS = 85_000
const DEFAULT_LOGOUT_TIMEOUT_MS = 45_000
// Permit small host-clock disagreement while still bounding credentials to the
// server-declared maximum short-lived token lifetime.
export const TRACE_CREDENTIAL_CLOCK_SKEW_MS = 30_000

const SAFE_ENV_KEYS = new Set([
  'APPDATA',
  'DBUS_SESSION_BUS_ADDRESS',
  'DISPLAY',
  'HERMES_AUTH_KEYRING_SERVICE',
  'HERMES_AUTH_LEGACY_KEYRING_SERVICE',
  'HERMES_AUTH_RUNTIME_NAMESPACE',
  'HERMES_HOME',
  'HOME',
  'LANG',
  'LANGUAGE',
  'LC_ALL',
  'LC_CTYPE',
  'LOCALAPPDATA',
  'PATH',
  'SSH_CONNECTION',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'USERPROFILE',
  'WAYLAND_DISPLAY',
  'WINDIR',
  'XDG_CONFIG_HOME',
  'XDG_DATA_HOME',
  'XDG_RUNTIME_DIR'
])

const SAFE_REASONS = new Set([
  'interactive_login_required',
  'invalid_credentials',
  'rate_limited',
  'runtime_unavailable',
  'server_unavailable',
  'session_expired',
  'session_rejected',
  'invalid_response',
  'invalid_session_credential',
  'signed_out',
  'session_revoked',
  'account_disabled',
  'account_revoked',
  'vault_unavailable'
])

const VALIDATION_REASONS = new Set([
  'rate_limited',
  'runtime_unavailable',
  'server_unavailable',
  'session_expired',
  'session_rejected',
  'invalid_response',
  'invalid_session_credential',
  'session_revoked',
  'account_disabled',
  'account_revoked',
  'vault_unavailable'
])

const TERMINAL_REASONS = new Set(['signed_out', 'session_revoked', 'account_disabled', 'account_revoked'])

export type AuthMethod = 'status' | 'login' | 'logout' | 'trace_token'

export type NativeClientContext = { installation_id: string; client_version: string }

export type ConnectionScope = {
  connection_id: string
  runtime_instance_id: string
  epoch: number
}

export type TraceTokenRequest = {
  installation_id: string
  client_version: string
  telemetry_schema_version: string
}

export type TraceCredential = {
  access_token: string
  expires_at: string
  expires_in: number
  installation_id: string
}

type AuthRequest =
  | { method: 'status'; params: NativeClientContext }
  | { method: 'login'; params: { username: string; password: string } & NativeClientContext }
  | { method: 'logout'; params: Record<string, never> }
  | { method: 'trace_token'; params: TraceTokenRequest }

type ChildLike = {
  stdin: NodeJS.WritableStream
  stdout: NodeJS.ReadableStream
  stderr: NodeJS.ReadableStream
  kill: () => unknown
  on: (event: string, listener: (...args: any[]) => void) => unknown
}

type SpawnChild = (
  command: string,
  args: string[],
  options: {
    cwd: string
    env: NodeJS.ProcessEnv
    stdio: ['pipe', 'pipe', 'pipe']
    windowsHide: boolean
  }
) => ChildLike

type DesktopAuthBridgeOptions = {
  clock?: () => number
  cwd: string
  env?: NodeJS.ProcessEnv
  onDiagnostic?: (message: string) => void
  nativeClientContext: NativeClientContext
  pythonExecutable: string
  spawnChild?: SpawnChild
  timeoutMs?: number
  loginTimeoutMs?: number
  logoutTimeoutMs?: number
}

type PendingRequest = {
  reject: (error: AuthBridgeError) => void
  request: AuthRequest
  resolve: (result: BridgeStatus | TraceCredential) => void
  timer: ReturnType<typeof setTimeout>
}

export class AuthBridgeError extends Error {
  readonly code: string
  readonly reason: string | null

  constructor(code: string, reason: string | null = null) {
    super(code)
    this.name = 'AuthBridgeError'
    this.code = code
    this.reason = reason
  }
}

export function buildAuthBridgeEnvironment(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}

  for (const key of SAFE_ENV_KEYS) {
    const value = source[key]

    if (typeof value === 'string') {
      env[key] = value
    }
  }

  return env
}

export function connectionScopeFromStatus(status: BridgeStatus, connectionId = 'local'): ConnectionScope {
  if (
    status.state !== 'authenticated' ||
    !connectionId ||
    !status.runtime_instance_id ||
    !Number.isSafeInteger(status.epoch) ||
    status.epoch < 0
  ) {
    throw new AuthBridgeError('auth_required', status.reason || 'interactive_login_required')
  }

  return requireAuthenticatedConnectionScope({
    connection_id: connectionId,
    runtime_instance_id: status.runtime_instance_id,
    epoch: status.epoch
  })
}

export function requireAuthenticatedConnectionScope(value: unknown): ConnectionScope {
  if (
    !isPlainObject(value) ||
    !sameKeys(value, ['connection_id', 'runtime_instance_id', 'epoch']) ||
    typeof value.connection_id !== 'string' ||
    value.connection_id.length === 0 ||
    value.connection_id.length > 128 ||
    typeof value.runtime_instance_id !== 'string' ||
    value.runtime_instance_id.length === 0 ||
    value.runtime_instance_id.length > 128 ||
    !Number.isSafeInteger(value.epoch) ||
    value.epoch < 0
  ) {
    throw new AuthBridgeError('auth_required', 'interactive_login_required')
  }

  return value as ConnectionScope
}

export class DesktopAuthBridge {
  private readonly child: ChildLike
  private readonly clock: () => number
  private readonly onDiagnostic: (message: string) => void
  private readonly pending = new Map<string, PendingRequest>()
  private readonly loginTimeoutMs: number
  private readonly logoutTimeoutMs: number
  private readonly timeoutMs: number
  private readonly nativeClientContext: NativeClientContext
  private nextRequestId = 0n
  private stdoutBuffer = Buffer.alloc(0)
  private unavailable = false

  constructor(options: DesktopAuthBridgeOptions) {
    this.clock = options.clock ?? Date.now
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.loginTimeoutMs = options.loginTimeoutMs ?? options.timeoutMs ?? DEFAULT_LOGIN_TIMEOUT_MS
    this.logoutTimeoutMs = options.logoutTimeoutMs ?? options.timeoutMs ?? DEFAULT_LOGOUT_TIMEOUT_MS
    this.onDiagnostic = options.onDiagnostic ?? (() => {})
    if (!isNativeClientContext(options.nativeClientContext)) {
      throw new AuthBridgeError('invalid_request')
    }
    this.nativeClientContext = { ...options.nativeClientContext }

    const spawnChild: SpawnChild =
      options.spawnChild ?? ((command, args, childOptions) => spawn(command, args, childOptions) as ChildLike)

    this.child = spawnChild(options.pythonExecutable, ['-m', 'hermes_cli.client_auth.bridge'], {
      cwd: options.cwd,
      env: buildAuthBridgeEnvironment(options.env),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true
    })

    this.child.stdout.on('data', chunk => this.consumeStdout(chunk))
    this.child.stdout.on('end', () => this.failRuntime())
    this.child.stdout.on('error', () => this.failRuntime())
    this.child.stdin.on('error', () => this.failRuntime())
    // stderr is deliberately drained and discarded. It is never promoted into
    // a renderer-visible error or diagnostic because Python/keyring failures
    // can contain session material.
    this.child.stderr.on('data', () => {})
    this.child.stderr.on('error', () => this.failRuntime())
    this.child.on('error', () => this.failRuntime())
    this.child.on('exit', () => this.failRuntime())
    this.child.on('close', () => this.failRuntime())
  }

  isUnavailable(): boolean {
    return this.unavailable
  }

  status(): Promise<BridgeStatus> {
    return this.invoke({ method: 'status', params: this.nativeClientContext })
  }

  login(username: string, password: string): Promise<BridgeStatus> {
    return this.invoke({ method: 'login', params: { username, password, ...this.nativeClientContext } })
  }

  logout(): Promise<BridgeStatus> {
    return this.invoke({ method: 'logout', params: {} })
  }

  traceToken(request: TraceTokenRequest): Promise<TraceCredential> {
    return this.invoke({ method: 'trace_token', params: request })
  }

  invoke(request: Exclude<AuthRequest, { method: 'trace_token' }>): Promise<BridgeStatus>
  invoke(request: Extract<AuthRequest, { method: 'trace_token' }>): Promise<TraceCredential>
  invoke(request: AuthRequest): Promise<BridgeStatus | TraceCredential> {
    if (!isValidRequest(request)) {
      return Promise.reject(new AuthBridgeError('invalid_request'))
    }

    if (this.unavailable) {
      return Promise.reject(runtimeUnavailable())
    }

    this.nextRequestId += 1n
    const id = this.nextRequestId.toString()

    if (id.length > MAX_REQUEST_ID_LENGTH) {
      this.failRuntime()

      return Promise.reject(runtimeUnavailable())
    }

    return new Promise<BridgeStatus | TraceCredential>((resolve, reject) => {
      const timer = setTimeout(
        () => this.failRuntime(),
        request.method === 'login'
          ? this.loginTimeoutMs
          : request.method === 'logout'
            ? this.logoutTimeoutMs
            : this.timeoutMs
      )

      this.pending.set(id, { reject, request, resolve, timer })

      const frame = JSON.stringify({
        version: PROTOCOL_VERSION,
        id,
        method: request.method,
        params: request.params
      })

      try {
        this.child.stdin.write(`${frame}\n`)
      } catch {
        this.failRuntime()
      }
    })
  }

  close(): void {
    if (!this.unavailable) {
      this.unavailable = true
      this.rejectAll(runtimeUnavailable())
      this.child.kill()
    }
  }

  private consumeStdout(chunk: unknown): void {
    if (this.unavailable) {
      return
    }

    let bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk))

    while (bytes.length > 0) {
      const newline = bytes.indexOf(0x0a)

      if (newline === -1) {
        if (this.stdoutBuffer.length + bytes.length > MAX_LINE_BYTES) {
          this.failRuntime()

          return
        }

        this.stdoutBuffer = Buffer.concat([this.stdoutBuffer, bytes])

        return
      }

      const segment = bytes.subarray(0, newline)

      if (this.stdoutBuffer.length + segment.length > MAX_LINE_BYTES) {
        this.failRuntime()

        return
      }

      const line = this.stdoutBuffer.length > 0 ? Buffer.concat([this.stdoutBuffer, segment]) : segment
      this.stdoutBuffer = Buffer.alloc(0)
      bytes = bytes.subarray(newline + 1)

      if (line.length === 0 || !this.consumeResponse(line)) {
        this.failRuntime()

        return
      }
    }
  }

  private consumeResponse(line: Buffer): boolean {
    let response: unknown

    try {
      response = JSON.parse(line.toString('utf8'))
    } catch {
      return false
    }

    if (!isPlainObject(response) || response.version !== PROTOCOL_VERSION || typeof response.id !== 'string') {
      return false
    }

    const pending = this.pending.get(response.id)

    if (!pending) {
      return false
    }

    if (Object.hasOwn(response, 'result')) {
      const validResult =
        pending.request.method === 'trace_token'
          ? isTraceCredential(response.result, pending.request.params.installation_id, this.clock())
          : isBridgeStatus(response.result)

      if (Object.keys(response).length !== 3 || !validResult) {
        return false
      }

      clearTimeout(pending.timer)
      this.pending.delete(response.id)
      pending.resolve(response.result)

      return true
    }

    if (Object.keys(response).length !== 3 || !isBridgeRemoteError(response.error)) {
      return false
    }

    clearTimeout(pending.timer)
    this.pending.delete(response.id)
    pending.reject(new AuthBridgeError(response.error.code.toLowerCase(), response.error.reason ?? null))

    return true
  }

  private failRuntime(): void {
    if (this.unavailable) {
      return
    }

    this.unavailable = true
    this.stdoutBuffer = Buffer.alloc(0)
    this.rejectAll(runtimeUnavailable())
    this.onDiagnostic('auth bridge runtime_unavailable')

    try {
      this.child.kill()
    } catch {
      // The bridge is already unavailable; a failed kill cannot relax the gate.
    }
  }

  private rejectAll(error: AuthBridgeError): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }

    this.pending.clear()
  }
}

function runtimeUnavailable(): AuthBridgeError {
  return new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
}

function isValidRequest(value: unknown): value is AuthRequest {
  if (!isPlainObject(value) || Object.keys(value).length !== 2 || !isPlainObject(value.params)) {
    return false
  }

  const paramKeys = Object.keys(value.params)

  if (value.method === 'status') {
    return isNativeClientContext(value.params)
  }

  if (value.method === 'logout') {
    return paramKeys.length === 0
  }

  if (value.method === 'trace_token') {
    return (
      sameKeys(value.params, ['installation_id', 'client_version', 'telemetry_schema_version']) &&
      isUuidV4(value.params.installation_id) &&
      typeof value.params.client_version === 'string' &&
      /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/.test(value.params.client_version) &&
      typeof value.params.telemetry_schema_version === 'string' &&
      /^[1-9][0-9]{0,15}$/.test(value.params.telemetry_schema_version)
    )
  }

  return (
    value.method === 'login' &&
    paramKeys.length === 4 &&
    Object.hasOwn(value.params, 'username') &&
    Object.hasOwn(value.params, 'password') &&
    typeof value.params.username === 'string' &&
    value.params.username.trim().length > 0 &&
    value.params.username.length <= 150 &&
    typeof value.params.password === 'string' &&
    value.params.password.length > 0 &&
    value.params.password.length <= 4096 &&
    hasValidNativeClientContextFields(value.params)
  )
}

export function traceCredentialExpiresAt(
  value: unknown,
  expectedInstallationId: string | undefined,
  now: number = Date.now()
): number | null {
  if (
    !Number.isSafeInteger(now) ||
    !isPlainObject(value) ||
    !sameKeys(value, ['access_token', 'expires_at', 'expires_in', 'installation_id'])
  ) {
    return null
  }

  if (
    typeof value.access_token !== 'string' ||
    value.access_token.length < 20 ||
    value.access_token.length > 4096 ||
    /[\r\n]/.test(value.access_token) ||
    typeof value.expires_at !== 'string' ||
    value.expires_at.length > 128 ||
    !Number.isSafeInteger(value.expires_in) ||
    value.expires_in < 1 ||
    value.expires_in > 900 ||
    typeof value.installation_id !== 'string' ||
    !isUuidV4(value.installation_id) ||
    (expectedInstallationId !== undefined && value.installation_id !== expectedInstallationId)
  ) {
    return null
  }

  const expiresAt = parseRfc3339Epoch(value.expires_at)
  const latest = now + value.expires_in * 1_000 + TRACE_CREDENTIAL_CLOCK_SKEW_MS

  if (expiresAt === null || !Number.isSafeInteger(latest) || expiresAt <= now || expiresAt > latest) {
    return null
  }

  return expiresAt
}

function isTraceCredential(value: unknown, expectedInstallationId: string, now: number): value is TraceCredential {
  return traceCredentialExpiresAt(value, expectedInstallationId, now) !== null
}

function parseRfc3339Epoch(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value)

  if (!match) {
    return null
  }

  const year = Number(match[1])
  const month = Number(match[2]) - 1
  const day = Number(match[3])
  const hour = Number(match[4])
  const minute = Number(match[5])
  const second = Number(match[6])
  const fraction = match[7] ?? ''
  // Round fractional seconds up to the next millisecond so an unrepresentable
  // sub-millisecond remainder can never make an overlong token look valid.
  const millisecond = Number(fraction.slice(0, 3).padEnd(3, '0')) + (/[1-9]/.test(fraction.slice(3)) ? 1 : 0)
  const offsetSign = match[9]
  const offsetHours = match[10] === undefined ? 0 : Number(match[10])
  const offsetMinutes = match[11] === undefined ? 0 : Number(match[11])

  if (
    year < 100 ||
    month < 0 ||
    month > 11 ||
    day < 1 ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHours > 23 ||
    offsetMinutes > 59
  ) {
    return null
  }

  const localBase = Date.UTC(year, month, day, hour, minute, second)
  const localDate = new Date(localBase)

  if (
    !Number.isSafeInteger(localBase) ||
    localDate.getUTCFullYear() !== year ||
    localDate.getUTCMonth() !== month ||
    localDate.getUTCDate() !== day ||
    localDate.getUTCHours() !== hour ||
    localDate.getUTCMinutes() !== minute ||
    localDate.getUTCSeconds() !== second
  ) {
    return null
  }

  const local = localBase + millisecond

  if (!Number.isSafeInteger(local)) {
    return null
  }

  const offset = (offsetHours * 60 + offsetMinutes) * 60 * 1_000
  const epoch = match[8] === 'Z' ? local : local - (offsetSign === '+' ? offset : -offset)

  return Number.isSafeInteger(epoch) ? epoch : null
}

function isUuidV4(value: unknown): value is string {
  return (
    typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  )
}

function isNativeClientContext(value: unknown): value is NativeClientContext {
  return (
    isPlainObject(value) &&
    sameKeys(value, ['installation_id', 'client_version']) &&
    hasValidNativeClientContextFields(value)
  )
}

function hasValidNativeClientContextFields(value: Record<string, unknown>): boolean {
  return (
    isUuidV4(value.installation_id) &&
    typeof value.client_version === 'string' &&
    /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/.test(value.client_version)
  )
}

function isPlainObject(value: unknown): value is Record<string, any> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isBridgeStatus(value: unknown): value is BridgeStatus {
  if (
    !isPlainObject(value) ||
    (!sameKeys(value, [
      'state',
      'username',
      'account_id',
      'session_id',
      'installation_id',
      'principal_key',
      'predecessor_principal_key',
      'runtime_instance_id',
      'epoch',
      'valid_until',
      'validation_state',
      'validation_reason',
      'last_validated_at',
      'legacy',
      'reason'
    ]) &&
      !sameKeys(value, [
        'state',
        'username',
        'account_id',
        'session_id',
        'installation_id',
        'principal_key',
        'runtime_instance_id',
        'epoch',
        'valid_until',
        'validation_state',
        'validation_reason',
        'last_validated_at',
        'legacy',
        'reason'
      ]))
  ) {
    return false
  }

  return (
    ['checking', 'authenticated', 'signed_out', 'locked'].includes(value.state) &&
    (value.username === null ||
      (typeof value.username === 'string' && value.username.length > 0 && value.username.length <= 150)) &&
    isOptionalBoundedString(value.account_id) &&
    isOptionalBoundedString(value.session_id) &&
    isOptionalBoundedString(value.installation_id) &&
    isOptionalBoundedString(value.principal_key) &&
    (value.predecessor_principal_key === undefined || isOptionalBoundedString(value.predecessor_principal_key)) &&
    typeof value.runtime_instance_id === 'string' &&
    value.runtime_instance_id.length > 0 &&
    value.runtime_instance_id.length <= 128 &&
    Number.isSafeInteger(value.epoch) &&
    value.epoch >= 0 &&
    typeof value.valid_until === 'number' &&
    Number.isFinite(value.valid_until) &&
    value.valid_until >= 0 &&
    ['unknown', 'validating', 'online', 'degraded'].includes(value.validation_state) &&
    (value.validation_reason === null ||
      (typeof value.validation_reason === 'string' && VALIDATION_REASONS.has(value.validation_reason))) &&
    (value.last_validated_at === null ||
      (typeof value.last_validated_at === 'string' &&
        value.last_validated_at.length > 0 &&
        value.last_validated_at.length <= 128)) &&
    typeof value.legacy === 'boolean' &&
    (value.reason === null || (typeof value.reason === 'string' && SAFE_REASONS.has(value.reason))) &&
    (value.state !== 'locked' || (typeof value.reason === 'string' && TERMINAL_REASONS.has(value.reason))) &&
    hasConsistentBridgeIdentity(value)
  )
}

function hasConsistentBridgeIdentity(value: Record<string, any>): boolean {
  const emptyIdentity =
    value.account_id === null &&
    value.session_id === null &&
    value.installation_id === null &&
    value.principal_key === null

  if (emptyIdentity) {
    return value.state !== 'authenticated' && value.legacy === false
  }

  if (value.legacy) {
    return (
      value.account_id === null &&
      value.session_id === null &&
      value.installation_id === null &&
      typeof value.principal_key === 'string' &&
      /^legacy:[0-9a-f]{64}$/.test(value.principal_key)
    )
  }

  if (
    value.predecessor_principal_key !== undefined &&
    value.predecessor_principal_key !== null &&
    !/^legacy:[0-9a-f]{64}$/.test(value.predecessor_principal_key)
  ) {
    return false
  }

  return (
    isUuidV4(value.account_id) &&
    isUuidV4(value.session_id) &&
    isUuidV4(value.installation_id) &&
    value.principal_key === `account:${value.account_id}`
  )
}

function isOptionalBoundedString(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length > 0 && value.length <= 256)
}

function isBridgeRemoteError(value: unknown): value is { code: string; reason?: string } {
  if (!isPlainObject(value)) {
    return false
  }

  const keys = Object.keys(value)

  return (
    (keys.length === 1 || (keys.length === 2 && Object.hasOwn(value, 'reason'))) &&
    typeof value.code === 'string' &&
    value.code.length > 0 &&
    value.code.length <= 64 &&
    (value.reason === undefined || (typeof value.reason === 'string' && SAFE_REASONS.has(value.reason)))
  )
}

function sameKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const actual = Object.keys(value)

  return actual.length === keys.length && keys.every(key => Object.hasOwn(value, key))
}
