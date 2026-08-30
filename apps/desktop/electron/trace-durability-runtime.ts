import { type ConnectionScope, sameConnectionScope } from './auth-bridge'
import type { TraceRecoveryReason } from './trace-forwarder'
import type { TraceIngressEndpoint } from './trace-ingress-facade'
import type { PendingLocalCommit, TraceOutboxStoreDiagnostics } from './trace-outbox-store'
import {
  type DurableReceipt,
  type DurableTraceBatch,
  sameTraceOwnerIdentity,
  type TraceEnvelopeInput,
  type TraceOwner,
  validateTraceOwner
} from './trace-outbox-types'

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

export class TraceDurabilityStartupError extends Error {
  readonly cause: unknown
  readonly code = 'trace_durability_startup_failed'

  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : 'trace_durability_startup_failed')
    this.name = 'TraceDurabilityStartupError'
    this.cause = cause
  }
}

export function isTraceDurabilityStartupError(error: unknown): error is TraceDurabilityStartupError {
  return error instanceof TraceDurabilityStartupError
}

export function safeTraceFailureCode(error: unknown): string {
  const code =
    typeof error === 'object' && error !== null && 'code' in error ? (error as { code?: unknown }).code : undefined

  return typeof code === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(code)
    ? code
    : 'trace_operation_failed'
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

type TraceDurabilityCounts = Pick<TraceOutboxStoreDiagnostics, 'pending' | 'pendingBytes'>

export type TraceDurabilityStore = {
  acknowledge(batchId: string, receipt: DurableReceipt): Promise<void>
  beginEnqueue(input: TraceEnvelopeInput): PendingLocalCommit
  close?(): Promise<void>
  diagnostics(): Promise<TraceOutboxStoreDiagnostics>
  peekEligible(now: number): Promise<DurableTraceBatch | undefined>
  quarantine(batchId: string, errorClass: string): Promise<void>
  quarantineInput(input: TraceEnvelopeInput, errorClass: string): Promise<DurableTraceBatch>
}

export type TraceDurabilityDiagnosticScope = {
  observeRecovery(counts: TraceDurabilityCounts, nextRetryAt: number | null): void
  observeStore(store: TraceDurabilityStore): TraceDurabilityStore
  storageFailed(error: unknown, counts?: Partial<TraceDurabilityCounts>): void
}

export class TraceDurabilityRuntime {
  private active: TraceDurabilitySession | null = null
  private diagnosticScope = 0
  private flight: Promise<TraceDurabilitySession> | null = null
  private generation = 0
  private lockFlight: Promise<void> | null = null
  private storageFailureGeneration: number | null = null

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

  bindDiagnostics(): TraceDurabilityDiagnosticScope {
    const generation = this.generation
    const scope = (this.diagnosticScope += 1)
    let backlogSeen = false
    let uploadDegraded = false

    const active = () => generation === this.generation && scope === this.diagnosticScope

    const reportStorageFailure = (error: unknown, counts?: Partial<TraceDurabilityCounts>) => {
      if (isTraceStoreControlFlowError(error) || !active() || this.storageFailureGeneration === generation) {
        return
      }

      this.storageFailureGeneration = generation
      this.emit({
        code: 'trace_storage_failed',
        errorClass: classifyTraceStorageFailure(error),
        ...safeTraceCounts(counts)
      })
    }

    const reportStoreFailure = async (error: unknown, store: TraceDurabilityStore) => {
      let counts: Partial<TraceDurabilityCounts> | undefined

      try {
        counts = await store.diagnostics()
      } catch {
        counts = undefined
      }

      reportStorageFailure(error, counts)
    }

    const observeMutation = <T>(operation: () => Promise<T>, store: TraceDurabilityStore): Promise<T> => {
      let result: Promise<T>

      try {
        result = operation()
      } catch (error) {
        reportStorageFailure(error)
        throw error
      }

      return result.catch(async error => {
        await reportStoreFailure(error, store)
        throw error
      })
    }

    const observeStore = (store: TraceDurabilityStore): TraceDurabilityStore => ({
      acknowledge: (batchId, receipt) => observeMutation(() => store.acknowledge(batchId, receipt), store),
      beginEnqueue: input => {
        let pending: PendingLocalCommit

        try {
          pending = store.beginEnqueue(input)
        } catch (error) {
          reportStorageFailure(error)
          throw error
        }

        return {
          ...pending,
          cancelForGatewayReceipt: receipt =>
            observeMutation(() => pending.cancelForGatewayReceipt(receipt), store),
          durable: pending.durable.catch(async error => {
            await reportStoreFailure(error, store)
            throw error
          })
        }
      },
      close: store.close ? () => observeMutation(() => store.close!(), store) : undefined,
      diagnostics: () => observeMutation(() => store.diagnostics(), store),
      peekEligible: now => observeMutation(() => store.peekEligible(now), store),
      quarantine: (batchId, errorClass) =>
        observeMutation(() => store.quarantine(batchId, errorClass), store),
      quarantineInput: (input, errorClass) =>
        observeMutation(() => store.quarantineInput(input, errorClass), store)
    })

    return {
      observeRecovery: (counts, nextRetryAt) => {
        if (!active()) {
          return
        }

        const safeCounts = safeTraceCounts(counts)
        const pending = safeCounts.pending ?? 0

        if (pending > 0) {
          backlogSeen = true
        }

        if (pending > 0 && nextRetryAt !== null) {
          if (!uploadDegraded) {
            uploadDegraded = true
            this.emit({ code: 'trace_upload_degraded', ...safeCounts, retryAttempt: 1 })
          }
        } else if (uploadDegraded) {
          uploadDegraded = false
          this.emit({ code: 'trace_upload_recovered', ...safeCounts })
        }

        if (pending === 0 && backlogSeen) {
          backlogSeen = false
          this.emit({ code: 'trace_backlog_recovered', ...safeCounts })
        }
      },
      observeStore,
      storageFailed: reportStorageFailure
    }
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
      this.emit({ code: 'trace_owner_rebound' })

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
    this.storageFailureGeneration = null
    this.emit({ code: 'trace_admission_ready' })

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

    this.emit({ code: 'trace_terminal_locked' })
  }

  private emit(event: TraceDurabilityDiagnostic): void {
    try {
      this.diagnostic(event)
    } catch {
      // Diagnostics must never change Trace durability behavior.
    }
  }
}

function isTraceStoreControlFlowError(error: unknown): boolean {
  return (
    error instanceof Error && (error.message === 'local_commit_cancelled' || error.message === 'trace_outbox_closed')
  )
}

function classifyTraceStorageFailure(error: unknown): string {
  const code = typeof error === 'object' && error !== null ? (error as NodeJS.ErrnoException).code : undefined
  const message = error instanceof Error ? error.message : ''

  if (code === 'ENOSPC') {
    return 'disk_full'
  }

  if (code === 'EDQUOT') {
    return 'quota_exceeded'
  }

  if (message === 'secure_key_storage_unavailable') {
    return 'secure_key_storage_unavailable'
  }

  if (
    message.startsWith('invalid_journal_') ||
    message.startsWith('invalid_trace_outbox_key') ||
    message.startsWith('trace_outbox_journal_')
  ) {
    return 'journal_integrity_failure'
  }

  return 'storage_io_failure'
}

function safeTraceCounts(counts: Partial<TraceDurabilityCounts> | undefined): Partial<TraceDurabilityCounts> {
  const safe: Partial<TraceDurabilityCounts> = {}
  const pending = counts?.pending
  const pendingBytes = counts?.pendingBytes

  if (typeof pending === 'number' && Number.isSafeInteger(pending) && pending >= 0) {
    safe.pending = pending
  }

  if (typeof pendingBytes === 'number' && Number.isSafeInteger(pendingBytes) && pendingBytes >= 0) {
    safe.pendingBytes = pendingBytes
  }

  return safe
}

export class TraceDurabilityCoordinator {
  private readonly createSession: TraceDurabilityCoordinatorOptions['createSession']
  private readonly facade: TraceDurabilityFacadeAdapter
  private facadeReady = false
  private generation = 0
  private lockFlight: Promise<void> | null = null
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

        try {
          await this.onAdmissionReady()
        } catch {
          // Durable admission is already live. Publishing its descriptor to
          // an existing backend is best-effort and has its own retry path.
        }

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

  lock(_reason: string, flushMs = 3_000): Promise<void> {
    if (this.lockFlight !== null) {
      return this.lockFlight
    }

    this.generation += 1
    this.facadeReady = false
    const publication = this.publication

    this.publication = null
    publication?.reject(new Error('trace_activation_superseded'))
    this.facade.detach()
    this.facade.rotateBearer()
    const locking = this.runtime.lock(flushMs)
    this.lockFlight = locking

    const clearLockFlight = () => {
      if (this.lockFlight === locking) {
        this.lockFlight = null
      }
    }

    void locking.then(clearLockFlight, clearLockFlight)

    return locking
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
