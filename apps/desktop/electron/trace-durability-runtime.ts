import { type ConnectionScope, sameConnectionScope } from './auth-bridge'
import type { TraceRecoveryReason } from './trace-forwarder'
import type { TraceIngressEndpoint } from './trace-ingress-facade'
import { sameTraceOwnerIdentity, type TraceOwner, validateTraceOwner } from './trace-outbox-types'

export type TraceDurabilityContext = {
  ingress: TraceIngressEndpoint
  owner: TraceOwner
  scope: ConnectionScope
}

export type TraceDurabilitySession = {
  compactIfIdle(): Promise<boolean>
  context(): TraceDurabilityContext
  rebind(owner: TraceOwner, scope: ConnectionScope): void
  stop(flushMs: number): Promise<void>
  trigger(reason: TraceRecoveryReason): void
}

export type TraceActivation = {
  context: TraceDurabilityContext
  kind: 'created' | 'rebound' | 'reused'
}

export type TraceDurabilityDiagnostic = {
  code:
    | 'trace_admission_ready'
    | 'trace_owner_rebound'
    | 'trace_upload_degraded'
    | 'trace_upload_recovered'
    | 'trace_storage_failed'
    | 'trace_terminal_locked'
    | 'trace_backlog_recovered'
  errorClass?: string
  pending?: number
  pendingBytes?: number
  retryAttempt?: number
}

export class TraceDurabilityRuntime {
  private active: TraceDurabilitySession | null = null
  private flight: Promise<TraceDurabilitySession> | null = null
  private generation = 0
  private lockFlight: Promise<void> | null = null

  constructor(private readonly diagnostic: (event: TraceDurabilityDiagnostic) => void = () => {}) {}

  current(): TraceDurabilityContext | null {
    const value = this.active?.context()

    return value
      ? {
          ingress: { ...value.ingress },
          owner: { ...value.owner },
          scope: { ...value.scope }
        }
      : null
  }

  async activate(
    requested: { owner: TraceOwner; scope: ConnectionScope },
    create: () => Promise<TraceDurabilitySession>
  ): Promise<TraceActivation> {
    const activationGeneration = this.generation
    const locking = this.lockFlight

    if (locking !== null) {
      await locking.catch(() => undefined)

      throw new Error('trace_activation_superseded')
    }

    const owner = validateTraceOwner(requested.owner).owner
    const requestedScope = { ...requested.scope }
    const current = this.active?.context()

    if (current) {
      if (current.owner.accountKey !== owner.accountKey || current.owner.installationId !== owner.installationId) {
        throw new Error('trace_account_switch_requires_lock')
      }

      if (sameTraceOwnerIdentity(current.owner, owner) && sameConnectionScope(current.scope, requestedScope)) {
        return { context: this.current()!, kind: 'reused' }
      }

      this.active!.rebind(owner, requestedScope)
      this.diagnostic({ code: 'trace_owner_rebound' })

      return { context: this.current()!, kind: 'rebound' }
    }

    const currentFlight = this.flight

    if (currentFlight !== null) {
      await currentFlight
      this.requireActivationGeneration(activationGeneration)

      return this.activate({ owner, scope: requestedScope }, create)
    }

    const generation = this.generation
    const flight = this.createAndPublish(generation, create)
    this.flight = flight

    try {
      await flight
      this.requireActivationGeneration(activationGeneration)
      const context = this.current()

      if (context === null) {
        throw new Error('trace_activation_superseded')
      }

      return { context, kind: 'created' }
    } finally {
      if (this.flight === flight) {
        this.flight = null
      }
    }
  }

  trigger(reason: TraceRecoveryReason): void {
    this.active?.trigger(reason)
  }

  compactIfIdle(): Promise<boolean> {
    return this.active?.compactIfIdle() ?? Promise.resolve(false)
  }

  lock(flushMs = 3_000): Promise<void> {
    if (this.lockFlight !== null) {
      return this.lockFlight
    }

    this.generation += 1
    const flight = this.flight
    const session = this.active
    this.active = null

    const locking = this.stopLockedSession(flight, session, flushMs)
    this.lockFlight = locking

    const clearLockFlight = () => {
      if (this.lockFlight === locking) {
        this.lockFlight = null
      }
    }

    void locking.then(clearLockFlight, clearLockFlight)

    return locking
  }

  private async createAndPublish(
    generation: number,
    create: () => Promise<TraceDurabilitySession>
  ): Promise<TraceDurabilitySession> {
    const session = await create()

    if (generation !== this.generation) {
      await session.stop(0)
      throw new Error('trace_activation_superseded')
    }

    this.active = session
    this.diagnostic({ code: 'trace_admission_ready' })

    return session
  }

  private requireActivationGeneration(generation: number): void {
    if (generation !== this.generation) {
      throw new Error('trace_activation_superseded')
    }
  }

  private async stopLockedSession(
    flight: Promise<TraceDurabilitySession> | null,
    session: TraceDurabilitySession | null,
    flushMs: number
  ): Promise<void> {
    await flight?.catch(() => undefined)

    if (session !== null) {
      await session.stop(flushMs)
    }

    this.diagnostic({ code: 'trace_terminal_locked' })
  }
}
