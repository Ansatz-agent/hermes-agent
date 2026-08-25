import {
  AuthBridgeError,
  type BridgeStatus,
  type ConnectionScope,
  connectionScopeFromStatus,
  requireAuthenticatedConnectionScope
} from './auth-bridge'
import type { ChannelAuthPolicy } from './guarded-ipc'

const LOCAL_CONNECTION_ID = 'local'
const DEFAULT_POLL_INTERVAL_MS = 15_000

const SAFE_REASONS = new Set([
  'interactive_login_required',
  'invalid_credentials',
  'rate_limited',
  'runtime_unavailable',
  'server_unavailable',
  'invalid_response',
  'invalid_session_credential',
  'session_expired',
  'session_rejected',
  'signed_out',
  'vault_unavailable'
])

const EXPLICIT_TERMINAL_REASONS = new Set(['account_disabled', 'account_revoked', 'session_revoked'])

type AuthBridgeLike = {
  status: () => Promise<BridgeStatus>
  login: (username: string, password: string) => Promise<BridgeStatus>
  logout: () => Promise<BridgeStatus>
}

type AuthBridgeRecovery = (
  connectionId: string,
  failedBridge: AuthBridgeLike
) => Promise<AuthBridgeLike | null> | AuthBridgeLike | null

type AuthRefreshOptions = {
  recoverRuntime?: boolean
}

type AuthCoordinatorOptions = {
  clock?: () => number
  cleanup?: (connectionId: string, status: BridgeStatus) => Promise<void> | void
  pollIntervalMs?: number
  recoverBridge?: AuthBridgeRecovery
}

type StatusListener = (status: BridgeStatus, connectionId: string) => void

export class CoordinatorAuthRequiredError extends Error {
  readonly code = 'AUTH_REQUIRED'

  constructor() {
    super('AUTH_REQUIRED')
    this.name = 'CoordinatorAuthRequiredError'
  }
}

export class AuthCoordinator {
  private readonly bridges = new Map<string, AuthBridgeLike>()
  private readonly clock: () => number
  private readonly cleanup: (connectionId: string, status: BridgeStatus) => Promise<void> | void
  private readonly generations = new Map<string, number>()
  private readonly listeners = new Set<StatusListener>()
  private readonly pollIntervalMs: number
  private readonly recoverBridge: AuthBridgeRecovery | null
  private readonly scopes = new Map<string, ConnectionScope>()
  private readonly statuses = new Map<string, BridgeStatus>()
  private operationTail: Promise<void> = Promise.resolve()
  private pollTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = false

  constructor(bridge: AuthBridgeLike, options: AuthCoordinatorOptions = {}) {
    this.bridges.set(LOCAL_CONNECTION_ID, bridge)
    // The Python bridge translates its monotonic runtime lease into Unix epoch
    // seconds before crossing the process (or SSH) boundary. A local uptime is
    // not comparable to Python's monotonic clock and can also have a different
    // origin on macOS after sleep.
    this.clock = options.clock ?? (() => Date.now() / 1000)
    this.cleanup = options.cleanup ?? (() => {})
    this.pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS
    this.recoverBridge = options.recoverBridge ?? null
    this.generations.set(LOCAL_CONNECTION_ID, 0)
    this.statuses.set(LOCAL_CONNECTION_ID, checkingStatus())
  }

  async start(): Promise<BridgeStatus> {
    this.stopped = false
    const status = await this.refresh()
    this.schedulePoll()

    return status
  }

  stop(): void {
    this.stopped = true

    if (this.pollTimer) {
      clearTimeout(this.pollTimer)
      this.pollTimer = null
    }
  }

  status(connectionId = LOCAL_CONNECTION_ID): BridgeStatus {
    return this.statuses.get(connectionId) ?? lockedStatus('interactive_login_required')
  }

  scope(connectionId = LOCAL_CONNECTION_ID): ConnectionScope | null {
    return this.scopes.get(connectionId) ?? null
  }

  isAuthenticated(connectionId = LOCAL_CONNECTION_ID): boolean {
    const status = this.status(connectionId)

    if (connectionId === LOCAL_CONNECTION_ID) {
      return isLocallyAuthorized(status) && this.scopes.has(connectionId)
    }

    return status.state === 'authenticated' && status.valid_until > this.clock() && this.scopes.has(connectionId)
  }

  hasConnection(connectionId: string): boolean {
    return this.bridges.has(connectionId)
  }

  subscribe(listener: StatusListener): () => void {
    this.listeners.add(listener)

    return () => this.listeners.delete(listener)
  }

  async refresh(connectionId = LOCAL_CONNECTION_ID, options: AuthRefreshOptions = {}): Promise<BridgeStatus> {
    const generation = this.generation(connectionId)

    return this.runExclusive(async () => {
      const bridge = this.bridges.get(connectionId)

      if (!bridge) {
        return this.applyFailure(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'), connectionId)
      }

      try {
        return await this.applyStatus(await bridge.status(), connectionId, generation)
      } catch (error) {
        const reason = safeReason(error)
        const failed = await this.applyFailure(error, connectionId, generation)

        if (
          connectionId !== LOCAL_CONNECTION_ID ||
          !options.recoverRuntime ||
          reason !== 'runtime_unavailable' ||
          !this.recoverBridge
        ) {
          return failed
        }

        try {
          const replacement = await this.recoverBridge(connectionId, bridge)

          if (!replacement) {
            return failed
          }

          this.bridges.set(connectionId, replacement)

          const checking =
            connectionId === LOCAL_CONNECTION_ID && this.scopes.has(connectionId) && isLocallyAuthorized(failed)
              ? { ...failed, validation_state: 'validating' as const, validation_reason: null }
              : checkingStatus()

          this.statuses.set(connectionId, checking)
          this.emit(checking, connectionId)

          return await this.applyStatus(await replacement.status(), connectionId, generation)
        } catch (recoveryError) {
          return this.applyFailure(recoveryError, connectionId, generation)
        }
      }
    })
  }

  async login(username: string, password: string, connectionId = LOCAL_CONNECTION_ID): Promise<BridgeStatus> {
    return this.runExclusive(async () => {
      const bridge = this.bridges.get(connectionId)

      if (!bridge) {
        return this.applyFailure(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'), connectionId)
      }

      try {
        return await this.applyStatus(await bridge.login(username, password), connectionId)
      } catch (error) {
        return this.applyFailure(error, connectionId)
      }
    })
  }

  async logout(connectionId = LOCAL_CONNECTION_ID): Promise<BridgeStatus> {
    return this.runExclusive(async () => {
      const previousScope = this.scopes.get(connectionId)

      if (previousScope) {
        const locked = lockFrom(this.status(connectionId), 'signed_out')
        this.scopes.delete(connectionId)
        this.advanceGeneration(connectionId)
        this.statuses.set(connectionId, locked)
        this.emit(locked, connectionId)
        await this.cleanup(connectionId, locked)
      }

      const bridge = this.bridges.get(connectionId)

      if (!bridge) {
        return this.applyFailure(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'), connectionId)
      }

      try {
        return await this.applyStatus(await bridge.logout(), connectionId)
      } catch (error) {
        return this.applyFailure(error, connectionId)
      }
    })
  }

  async registerConnection(connectionId: string, bridge: AuthBridgeLike): Promise<BridgeStatus> {
    requireConnectionId(connectionId)

    if (connectionId === LOCAL_CONNECTION_ID) {
      throw new TypeError('The local auth connection is owned by the coordinator constructor')
    }

    return this.runExclusive(async () => {
      const previousScope = this.scopes.get(connectionId)

      if (previousScope) {
        const locked = lockFrom(this.status(connectionId), 'session_rejected')
        this.scopes.delete(connectionId)
        this.advanceGeneration(connectionId)
        this.statuses.set(connectionId, locked)
        this.emit(locked, connectionId)
        await this.cleanup(connectionId, locked)
      }

      this.bridges.set(connectionId, bridge)
      this.statuses.set(connectionId, checkingStatus())

      try {
        return await this.applyStatus(await bridge.status(), connectionId)
      } catch (error) {
        return this.applyFailure(error, connectionId)
      }
    })
  }

  async unregisterConnection(connectionId: string): Promise<void> {
    requireConnectionId(connectionId)

    if (connectionId === LOCAL_CONNECTION_ID) {
      throw new TypeError('The local auth connection cannot be unregistered')
    }

    await this.runExclusive(async () => {
      const previousScope = this.scopes.get(connectionId)
      const locked = lockFrom(this.status(connectionId), 'signed_out')
      this.scopes.delete(connectionId)
      this.advanceGeneration(connectionId)
      this.bridges.delete(connectionId)
      this.statuses.set(connectionId, locked)
      this.emit(locked, connectionId)

      if (previousScope) {
        await this.cleanup(connectionId, locked)
      }
    })
  }

  async require(policy: ChannelAuthPolicy, connectionId: string | null): Promise<void> {
    if (policy === 'auth-free') {
      return
    }

    if (policy === 'local') {
      await this.requireConnection(LOCAL_CONNECTION_ID)

      return
    }

    if (policy === 'connection') {
      await this.requireConnection(connectionId)

      return
    }

    await this.requireConnection(LOCAL_CONNECTION_ID)
    await this.requireConnection(connectionId)
  }

  async requireScope(scope: ConnectionScope | null): Promise<void> {
    let required: ConnectionScope

    try {
      required = requireAuthenticatedConnectionScope(scope)
    } catch {
      throw new CoordinatorAuthRequiredError()
    }

    const current = this.scopes.get(required.connection_id)

    if (!current || current.runtime_instance_id !== required.runtime_instance_id || current.epoch !== required.epoch) {
      throw new CoordinatorAuthRequiredError()
    }

    await this.requireConnection(required.connection_id)
  }

  private async requireConnection(connectionId: string | null): Promise<void> {
    if (!connectionId) {
      throw new CoordinatorAuthRequiredError()
    }

    const current = this.scopes.get(connectionId)
    const status = this.status(connectionId)

    if (
      !current ||
      (connectionId === LOCAL_CONNECTION_ID ? !isLocallyAuthorized(status) : status.state !== 'authenticated')
    ) {
      throw new CoordinatorAuthRequiredError()
    }

    if (connectionId !== LOCAL_CONNECTION_ID && status.valid_until <= this.clock()) {
      const locked = lockFrom(status, 'session_expired')
      this.scopes.delete(connectionId)
      this.advanceGeneration(connectionId)
      this.statuses.set(connectionId, locked)
      this.emit(locked, connectionId)
      await this.cleanup(connectionId, locked)
      throw new CoordinatorAuthRequiredError()
    }
  }

  private async applyStatus(status: BridgeStatus, connectionId: string, generation?: number): Promise<BridgeStatus> {
    if (generation !== undefined && generation !== this.generation(connectionId)) {
      return this.status(connectionId)
    }

    if (connectionId === LOCAL_CONNECTION_ID) {
      return this.applyLocalStatus(status)
    }

    const nextStatus =
      status.state === 'authenticated' && status.valid_until <= this.clock()
        ? lockFrom(status, 'session_expired')
        : status

    const previousScope = this.scopes.get(connectionId)

    if (nextStatus.state === 'authenticated') {
      const nextScope = connectionScopeFromStatus(nextStatus, connectionId)

      if (
        previousScope &&
        (previousScope.runtime_instance_id !== nextScope.runtime_instance_id || previousScope.epoch !== nextScope.epoch)
      ) {
        const locked = lockFrom(this.status(connectionId), 'session_rejected')
        this.scopes.delete(connectionId)
        this.advanceGeneration(connectionId)
        this.statuses.set(connectionId, locked)
        this.emit(locked, connectionId)
        await this.cleanup(connectionId, locked)
      }

      this.scopes.set(connectionId, nextScope)
      this.statuses.set(connectionId, nextStatus)
      this.emit(nextStatus, connectionId)

      return nextStatus
    }

    this.scopes.delete(connectionId)

    if (previousScope) {
      this.advanceGeneration(connectionId)
    }

    this.statuses.set(connectionId, nextStatus)
    this.emit(nextStatus, connectionId)

    if (previousScope) {
      await this.cleanup(connectionId, nextStatus)
    }

    return nextStatus
  }

  private async applyFailure(error: unknown, connectionId: string, generation?: number): Promise<BridgeStatus> {
    if (generation !== undefined && generation !== this.generation(connectionId)) {
      return this.status(connectionId)
    }

    const reason = safeReason(error)

    if (connectionId === LOCAL_CONNECTION_ID) {
      const previous = this.status(connectionId)

      if (this.scopes.has(connectionId) && isLocallyAuthorized(previous)) {
        const degraded = degradedFrom(previous, reason)
        this.statuses.set(connectionId, degraded)
        this.emit(degraded, connectionId)

        return degraded
      }
    }

    const status = lockFrom(this.status(connectionId), reason)
    const hadScope = this.scopes.delete(connectionId)

    if (hadScope) {
      this.advanceGeneration(connectionId)
    }

    this.statuses.set(connectionId, status)
    this.emit(status, connectionId)

    if (hadScope) {
      await this.cleanup(connectionId, status)
    }

    return status
  }

  private async applyLocalStatus(status: BridgeStatus): Promise<BridgeStatus> {
    const connectionId = LOCAL_CONNECTION_ID
    const previousScope = this.scopes.get(connectionId)
    const previousStatus = this.status(connectionId)

    if (isLocallyAuthorized(status)) {
      const nextScope = connectionScopeFromStatus(status, connectionId)

      if (previousScope && isLocallyAuthorized(previousStatus) && !sameAccount(previousStatus, status)) {
        const locked = lockFrom(previousStatus, 'session_rejected')
        this.scopes.delete(connectionId)
        this.advanceGeneration(connectionId)
        this.statuses.set(connectionId, locked)
        this.emit(locked, connectionId)
        await this.cleanup(connectionId, locked)
      }

      const currentScope = this.scopes.get(connectionId)
      const published = currentScope && sameAccount(previousStatus, status) ? withScope(status, currentScope) : status

      if (!currentScope) {
        this.advanceGeneration(connectionId)
      }

      this.scopes.set(connectionId, currentScope ?? nextScope)
      this.statuses.set(connectionId, published)
      this.emit(published, connectionId)

      return published
    }

    const reason = status.reason ?? status.validation_reason ?? 'runtime_unavailable'

    const matchingTerminal =
      EXPLICIT_TERMINAL_REASONS.has(reason) &&
      previousScope &&
      isMatchingExplicitTerminal(previousStatus, status, reason)

    if (sameExplicitTerminalSnapshot(previousStatus, status, reason)) {
      return previousStatus
    }

    if (status.state === 'signed_out' || matchingTerminal) {
      this.scopes.delete(connectionId)

      if (previousScope) {
        this.advanceGeneration(connectionId)
      }

      this.statuses.set(connectionId, status)
      this.emit(status, connectionId)

      if (previousScope) {
        await this.cleanup(connectionId, status)
      }

      return status
    }

    if (previousScope && isLocallyAuthorized(previousStatus)) {
      const degraded = degradedFrom(previousStatus, reason)
      this.statuses.set(connectionId, degraded)
      this.emit(degraded, connectionId)

      return degraded
    }

    this.statuses.set(connectionId, status)
    this.emit(status, connectionId)

    return status
  }

  private generation(connectionId: string): number {
    return this.generations.get(connectionId) ?? 0
  }

  private advanceGeneration(connectionId: string): void {
    this.generations.set(connectionId, this.generation(connectionId) + 1)
  }

  private emit(status: BridgeStatus, connectionId: string): void {
    for (const listener of this.listeners) {
      listener(status, connectionId)
    }
  }

  private runExclusive<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.operationTail.then(operation, operation)
    this.operationTail = result.then(
      () => undefined,
      () => undefined
    )

    return result
  }

  private schedulePoll(): void {
    if (this.stopped || this.pollIntervalMs <= 0) {
      return
    }

    this.pollTimer = setTimeout(() => {
      this.pollTimer = null
      const refreshes = [...this.bridges.keys()].map(connectionId => this.refresh(connectionId))
      void Promise.all(refreshes).finally(() => this.schedulePoll())
    }, this.pollIntervalMs)
    this.pollTimer.unref?.()
  }
}

function requireConnectionId(connectionId: string): void {
  if (
    typeof connectionId !== 'string' ||
    !connectionId ||
    connectionId.trim() !== connectionId ||
    connectionId.length > 128 ||
    [...connectionId].some(character => {
      const codepoint = character.codePointAt(0) ?? 0

      return codepoint < 0x20 || codepoint === 0x7f
    })
  ) {
    throw new TypeError('Invalid auth connection id')
  }
}

function safeReason(error: unknown): string {
  const reason = error instanceof AuthBridgeError ? error.reason : null

  return reason && SAFE_REASONS.has(reason) ? reason : 'runtime_unavailable'
}

export function isLocallyAuthorized(status: BridgeStatus): boolean {
  return status.state === 'authenticated' && Boolean(status.principal_key)
}

function sameAccount(left: BridgeStatus, right: BridgeStatus): boolean {
  return (
    Boolean(left.principal_key) && left.principal_key === right.principal_key && left.account_id === right.account_id
  )
}

function sameCurrentSession(left: BridgeStatus, right: BridgeStatus): boolean {
  return sameAccount(left, right) && Boolean(left.session_id) && left.session_id === right.session_id
}

function isMatchingExplicitTerminal(left: BridgeStatus, right: BridgeStatus, reason: string): boolean {
  if (!isLocallyAuthorized(left)) {
    return false
  }

  return reason === 'session_revoked' ? sameCurrentSession(left, right) : sameAccount(left, right)
}

function sameExplicitTerminalSnapshot(left: BridgeStatus, right: BridgeStatus, reason: string): boolean {
  return (
    left.state === 'locked' &&
    right.state === 'locked' &&
    EXPLICIT_TERMINAL_REASONS.has(reason) &&
    left.reason === reason &&
    left.account_id === right.account_id &&
    left.session_id === right.session_id &&
    left.principal_key === right.principal_key &&
    left.runtime_instance_id === right.runtime_instance_id &&
    left.epoch === right.epoch
  )
}

function withScope(status: BridgeStatus, scope: ConnectionScope): BridgeStatus {
  return {
    ...status,
    runtime_instance_id: scope.runtime_instance_id,
    epoch: scope.epoch
  }
}

function degradedFrom(status: BridgeStatus, reason: string): BridgeStatus {
  return {
    ...status,
    state: 'authenticated',
    validation_state: 'degraded',
    validation_reason: reason,
    reason: null
  }
}

function checkingStatus(): BridgeStatus {
  return {
    state: 'checking',
    username: null,
    account_id: null,
    session_id: null,
    installation_id: null,
    principal_key: null,
    runtime_instance_id: 'checking',
    epoch: 0,
    valid_until: 0,
    validation_state: 'validating',
    validation_reason: null,
    last_validated_at: null,
    legacy: false,
    reason: null
  }
}

function lockedStatus(reason: string): BridgeStatus {
  return {
    ...checkingStatus(),
    state: 'locked',
    runtime_instance_id: 'unavailable',
    reason
  }
}

function lockFrom(status: BridgeStatus, reason: string): BridgeStatus {
  return {
    ...status,
    state: 'locked',
    username: null,
    epoch: status.epoch + 1,
    valid_until: 0,
    validation_state: 'unknown',
    validation_reason: null,
    last_validated_at: null,
    legacy: false,
    reason
  }
}
