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

type TraceDurabilityFacadeAdapter = {
  detach(): void
  install(ingress: TraceIngressEndpoint): void
  rotateBearer(): void
}

type TraceDurabilityCoordinatorOptions = {
  createSession(owner: TraceOwner, scope: ConnectionScope): Promise<TraceDurabilitySession>
  facade: TraceDurabilityFacadeAdapter
  onAdmissionReady?: () => Promise<void> | void
  runtime: TraceDurabilityRuntime
}

type TraceFacadePublication = {
  promise: Promise<void>
  reject(error: unknown): void
  resolve(): void
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

export class TraceDurabilityCoordinator {
  private readonly createSession: TraceDurabilityCoordinatorOptions['createSession']
  private readonly facade: TraceDurabilityFacadeAdapter
  private facadeReady = false
  private generation = 0
  private readonly onAdmissionReady: NonNullable<TraceDurabilityCoordinatorOptions['onAdmissionReady']>
  private publication: TraceFacadePublication | null = null
  private readonly runtime: TraceDurabilityRuntime

  constructor(options: TraceDurabilityCoordinatorOptions) {
    this.createSession = options.createSession
    this.facade = options.facade
    this.onAdmissionReady = options.onAdmissionReady ?? (() => {})
    this.runtime = options.runtime
  }

  async activate(requested: { owner: TraceOwner; scope: ConnectionScope }): Promise<TraceActivation> {
    const generation = this.generation
    let publication = this.publication
    let ownsPublication = false

    if (publication === null && !this.facadeReady) {
      publication = this.createPublication()
      this.publication = publication
      ownsPublication = true
    }

    try {
      const activation = await this.runtime.activate(requested, () =>
        this.createSession(requested.owner, requested.scope)
      )

      this.requireGeneration(generation)

      if (ownsPublication) {
        const context = this.requireCurrentContext()

        this.facade.install(context.ingress)
        this.facadeReady = true
        await this.onAdmissionReady()
        this.requireGeneration(generation)
        publication.resolve()
      } else if (publication !== null) {
        await publication.promise
      }

      this.requireGeneration(generation)

      return { context: this.requireCurrentContext(), kind: activation.kind }
    } catch (error) {
      if (ownsPublication) {
        publication.reject(error)
      }

      throw error
    } finally {
      if (ownsPublication && this.publication === publication) {
        this.publication = null
      }
    }
  }

  applySameAccountOwner(owner: TraceOwner, scope?: ConnectionScope): Promise<TraceActivation> {
    const current = this.runtime.current()

    if (current === null) {
      return Promise.reject(new Error('trace_runtime_unavailable'))
    }

    return this.activate({ owner, scope: scope ?? current.scope })
  }

  async lock(_reason: string, flushMs = 3_000): Promise<void> {
    this.generation += 1
    this.facadeReady = false
    const publication = this.publication

    this.publication = null
    publication?.reject(new Error('trace_activation_superseded'))
    this.facade.detach()
    this.facade.rotateBearer()
    await this.runtime.lock(flushMs)
  }

  private createPublication(): TraceFacadePublication {
    let reject!: (error: unknown) => void
    let resolve!: () => void

    const promise = new Promise<void>((currentResolve, currentReject) => {
      reject = currentReject
      resolve = currentResolve
    })

    void promise.catch(() => {})

    return { promise, reject, resolve }
  }

  private requireCurrentContext(): TraceDurabilityContext {
    const context = this.runtime.current()

    if (context === null) {
      throw new Error('trace_activation_superseded')
    }

    return context
  }

  private requireGeneration(generation: number): void {
    if (generation !== this.generation) {
      throw new Error('trace_activation_superseded')
    }
  }
}
