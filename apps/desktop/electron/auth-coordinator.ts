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
  'session_expired',
  'session_rejected',
  'signed_out',
  'vault_unavailable'
])

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
    return this.runExclusive(async () => {
      const bridge = this.bridges.get(connectionId)

      if (!bridge) {
        return this.applyFailure(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'), connectionId)
      }

      try {
        return await this.applyStatus(await bridge.status(), connectionId)
      } catch (error) {
        const failed = await this.applyFailure(error, connectionId)

        if (
          connectionId !== LOCAL_CONNECTION_ID ||
          !options.recoverRuntime ||
          failed.reason !== 'runtime_unavailable' ||
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
          const checking = checkingStatus()
          this.statuses.set(connectionId, checking)
          this.emit(checking, connectionId)

          return await this.applyStatus(await replacement.status(), connectionId)
        } catch (recoveryError) {
          return this.applyFailure(recoveryError, connectionId)
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

    if (!current || status.state !== 'authenticated') {
      throw new CoordinatorAuthRequiredError()
    }

    if (status.valid_until <= this.clock()) {
      const locked = lockFrom(status, 'session_expired')
      this.scopes.delete(connectionId)
      this.statuses.set(connectionId, locked)
      this.emit(locked, connectionId)
      await this.cleanup(connectionId, locked)
      throw new CoordinatorAuthRequiredError()
    }
  }

  private async applyStatus(status: BridgeStatus, connectionId: string): Promise<BridgeStatus> {
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
    this.statuses.set(connectionId, nextStatus)
    this.emit(nextStatus, connectionId)

    if (previousScope) {
      await this.cleanup(connectionId, nextStatus)
    }

    return nextStatus
  }

  private async applyFailure(error: unknown, connectionId: string): Promise<BridgeStatus> {
    const reason = safeReason(error)
    const status = lockFrom(this.status(connectionId), reason)
    const hadScope = this.scopes.delete(connectionId)
    this.statuses.set(connectionId, status)
    this.emit(status, connectionId)

    if (hadScope) {
      await this.cleanup(connectionId, status)
    }

    return status
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

function checkingStatus(): BridgeStatus {
  return {
    state: 'checking',
    username: null,
    runtime_instance_id: 'checking',
    epoch: 0,
    valid_until: 0,
    session_expires_at: null,
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
    session_expires_at: null,
    reason
  }
}
