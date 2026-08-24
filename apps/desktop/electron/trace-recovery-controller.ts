import type { TraceRecoveryReason } from './trace-forwarder'
import { isCanonicalTraceAccountKey, type TraceOwner, validateTraceOwner } from './trace-outbox-types'

export const ALL_TRACE_RECOVERY_REASONS: readonly TraceRecoveryReason[] = [
  'enqueue',
  'startup',
  'timer',
  'renderer-online',
  'resume',
  'focus',
  'token-ready',
  'token-near-expiry',
  'upload-401',
  'upload-403'
]

type TraceRecoveryControllerOptions = {
  accountKey: string
  pump: () => Promise<void>
}

type TraceRecoveryTarget = {
  stop?: () => Promise<void> | void
  trigger(reason: TraceRecoveryReason): void
}

type TraceCredentialReadiness = {
  current(): Promise<unknown>
  expiresAt(): number | null
}

type TraceTimer = number | ReturnType<typeof setTimeout>

type TraceRecoveryLifecycleOptions = {
  clearTimer?: (timer: TraceTimer) => void
  clock?: () => number
  controller: TraceRecoveryTarget
  credentialProvider: TraceCredentialReadiness
  periodicMs?: number
  setTimer?: (callback: () => void, delay: number) => TraceTimer
}

const DEFAULT_PERIODIC_RECOVERY_MS = 30_000
const TOKEN_NEAR_EXPIRY_MS = 60_000

export function legacyTraceOwner(principalDigest: string, installationId: string): TraceOwner {
  return validateTraceOwner({
    accountId: null,
    accountKey: `legacy-${principalDigest}`,
    installationId,
    sessionId: null
  }).owner
}

export class TraceRecoveryController {
  readonly accountKey: string

  private readonly pump: () => Promise<void>
  private running = false
  private scheduled = false
  private rerun = false
  private stopped = false
  private readonly idleWaiters = new Set<() => void>()

  constructor(options: TraceRecoveryControllerOptions) {
    if (!isCanonicalTraceAccountKey(options.accountKey)) {
      throw new TypeError('invalid_account_key')
    }

    this.accountKey = options.accountKey
    this.pump = options.pump
  }

  trigger(_reason: TraceRecoveryReason): void {
    if (this.stopped) {
      return
    }

    if (this.running) {
      this.rerun = true

      return
    }

    if (this.scheduled) {
      return
    }

    this.scheduled = true
    queueMicrotask(() => void this.run())
  }

  async stop(): Promise<void> {
    this.stopped = true
    this.rerun = false
    await this.whenIdle()
  }

  whenIdle(): Promise<void> {
    if (!this.running && !this.scheduled) {
      return Promise.resolve()
    }

    return new Promise(resolve => this.idleWaiters.add(resolve))
  }

  private async run(): Promise<void> {
    this.scheduled = false

    if (this.stopped) {
      this.resolveIdle()

      return
    }

    this.running = true

    do {
      this.rerun = false

      try {
        await this.pump()
      } catch {
        // Recovery is best-effort background work. The durable head remains
        // available for the next lifecycle signal or timer.
      }
    } while (!this.stopped && this.rerun)

    this.running = false
    this.resolveIdle()
  }

  private resolveIdle(): void {
    if (this.running || this.scheduled) {
      return
    }

    for (const resolve of this.idleWaiters) {
      resolve()
    }

    this.idleWaiters.clear()
  }
}

export class TraceRecoveryLifecycle {
  private readonly clearTimer: (timer: TraceTimer) => void
  private readonly clock: () => number
  private readonly controller: TraceRecoveryTarget
  private readonly credentialProvider: TraceCredentialReadiness
  private readonly periodicMs: number
  private readonly setTimer: (callback: () => void, delay: number) => TraceTimer
  private periodicTimer: TraceTimer | null = null
  private retryTimer: TraceTimer | null = null
  private stopped = false
  private tokenFlight: Promise<void> | null = null
  private tokenTimer: TraceTimer | null = null

  constructor(options: TraceRecoveryLifecycleOptions) {
    if (
      !Number.isSafeInteger(options.periodicMs ?? DEFAULT_PERIODIC_RECOVERY_MS) ||
      (options.periodicMs ?? DEFAULT_PERIODIC_RECOVERY_MS) <= 0
    ) {
      throw new RangeError('invalid_trace_recovery_period')
    }

    this.clearTimer = options.clearTimer ?? clearTimeout
    this.clock = options.clock ?? Date.now
    this.controller = options.controller
    this.credentialProvider = options.credentialProvider
    this.periodicMs = options.periodicMs ?? DEFAULT_PERIODIC_RECOVERY_MS
    this.setTimer = options.setTimer ?? setTimeout
  }

  start(): void {
    if (this.stopped || this.periodicTimer !== null) {
      return
    }

    this.controller.trigger('startup')
    this.schedulePeriodic()
    this.acquireToken()
  }

  trigger(reason: TraceRecoveryReason): void {
    if (!this.stopped) {
      this.controller.trigger(reason)
    }
  }

  scheduleRetryAt(nextRetryAt: number | null): void {
    if (this.retryTimer !== null) {
      this.clearTimer(this.retryTimer)
      this.retryTimer = null
    }

    if (this.stopped || nextRetryAt === null || !Number.isSafeInteger(nextRetryAt)) {
      return
    }

    const delay = Math.max(0, nextRetryAt - this.clock())
    this.retryTimer = this.setTimer(() => {
      this.retryTimer = null
      this.trigger('timer')
    }, delay)
  }

  async stop(): Promise<void> {
    this.stopped = true

    for (const timer of [this.periodicTimer, this.retryTimer, this.tokenTimer]) {
      if (timer !== null) {
        this.clearTimer(timer)
      }
    }

    this.periodicTimer = null
    this.retryTimer = null
    this.tokenTimer = null

    // Closing the controller is synchronous; an already-running pump may
    // still be unwinding a credential request. The forwarder independently
    // closes admission and rechecks its owner after every await, so shutdown
    // must not wait for that remote request to time out.
    void Promise.resolve(this.controller.stop?.()).catch(() => {})
  }

  private acquireToken(): void {
    if (this.stopped || this.tokenFlight !== null) {
      return
    }

    const current = this.credentialProvider
      .current()
      .then(() => {
        if (this.stopped) {
          return
        }

        this.controller.trigger('token-ready')
        this.scheduleTokenExpiry()
      })
      .catch(() => {
        // A missing Trace credential pauses cloud recovery only. The periodic
        // timer will retry without changing local authentication or runtime.
      })

    this.tokenFlight = current
    void current.finally(() => {
      if (this.tokenFlight === current) {
        this.tokenFlight = null
      }
    })
  }

  private schedulePeriodic(): void {
    this.periodicTimer = this.setTimer(() => {
      this.periodicTimer = null
      this.trigger('timer')
      this.acquireToken()

      if (!this.stopped) {
        this.schedulePeriodic()
      }
    }, this.periodicMs)
  }

  private scheduleTokenExpiry(): void {
    if (this.tokenTimer !== null) {
      this.clearTimer(this.tokenTimer)
      this.tokenTimer = null
    }

    const expiresAt = this.credentialProvider.expiresAt()

    if (expiresAt === null || !Number.isSafeInteger(expiresAt)) {
      return
    }

    this.tokenTimer = this.setTimer(
      () => {
        this.tokenTimer = null
        this.trigger('token-near-expiry')
        this.acquireToken()
      },
      Math.max(0, expiresAt - TOKEN_NEAR_EXPIRY_MS - this.clock())
    )
  }
}
