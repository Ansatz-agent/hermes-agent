import { type TraceCredential, traceCredentialExpiresAt } from './auth-bridge'
import { type TraceOwner, validateTraceOwner } from './trace-outbox-types'

const EXPIRY_SKEW_MS = 60_000

export type TraceCredentialSource = {
  load(forceRefresh: boolean): Promise<TraceCredential>
}

export type TraceCredentialProvider = {
  current(options?: { forceRefresh?: boolean }): Promise<TraceCredential>
  invalidate(): void
  clear(): void
  expiresAt(): number | null
}

type BoundTraceCredentialLoader = (forceRefresh: boolean) => Promise<TraceCredential>

type TraceCredentialBinding = {
  generation: number
  loader: BoundTraceCredentialLoader
  owner: TraceOwner
}

export class RebindableTraceCredentialSource implements TraceCredentialSource {
  private binding: TraceCredentialBinding | null = null
  private generation = 0

  bind(owner: TraceOwner, loader: BoundTraceCredentialLoader): number {
    const next = validateTraceOwner(owner).owner
    const current = this.binding?.owner

    if (current && (current.accountKey !== next.accountKey || current.installationId !== next.installationId)) {
      throw new Error('trace_credential_account_mismatch')
    }

    this.generation += 1
    this.binding = { generation: this.generation, loader, owner: { ...next } }

    return this.generation
  }

  clear(): void {
    this.generation += 1
    this.binding = null
  }

  owner(): TraceOwner | null {
    return this.binding ? { ...this.binding.owner } : null
  }

  async load(forceRefresh: boolean): Promise<TraceCredential> {
    const binding = this.binding

    if (binding === null) {
      throw new Error('trace_credential_binding_unavailable')
    }

    const credential = await binding.loader(forceRefresh)

    if (this.binding !== binding || binding.generation !== this.generation) {
      throw new Error('trace_credential_binding_changed')
    }

    return credential
  }
}

type RefreshingTraceCredentialProviderOptions = {
  clock?: () => number
  installationId?: string
}

type CredentialFlight = {
  generation: number
  promise: Promise<TraceCredential>
}

export class RefreshingTraceCredentialProvider implements TraceCredentialProvider {
  private readonly clock: () => number
  private readonly installationId: string | undefined
  private credential: TraceCredential | null = null
  private credentialExpiresAt: number | null = null
  private forcedFlight: CredentialFlight | null = null
  private generation = 0
  private normalFlight: CredentialFlight | null = null

  constructor(
    private readonly source: TraceCredentialSource,
    options: RefreshingTraceCredentialProviderOptions = {}
  ) {
    this.clock = options.clock ?? Date.now
    this.installationId = options.installationId
  }

  current({ forceRefresh = false }: { forceRefresh?: boolean } = {}): Promise<TraceCredential> {
    return forceRefresh ? this.currentForced() : this.currentNormal()
  }

  invalidate(): void {
    this.generation += 1
    this.credential = null
    this.credentialExpiresAt = null
  }

  clear(): void {
    this.invalidate()
  }

  expiresAt(): number | null {
    return this.credentialExpiresAt
  }

  private currentNormal(): Promise<TraceCredential> {
    const generation = this.generation

    if (
      this.credential &&
      this.credentialExpiresAt !== null &&
      this.clock() < this.credentialExpiresAt - EXPIRY_SKEW_MS
    ) {
      return Promise.resolve(this.credential)
    }

    if (this.forcedFlight?.generation === generation) {
      return this.forcedFlight.promise
    }

    if (this.normalFlight?.generation === generation) {
      return this.normalFlight.promise
    }

    return this.startNormalFlight(generation)
  }

  private currentForced(): Promise<TraceCredential> {
    const generation = this.generation

    if (this.forcedFlight?.generation === generation) {
      return this.forcedFlight.promise
    }

    let flight: CredentialFlight
    const pendingNormal = this.normalFlight

    const promise = (async () => {
      await Promise.resolve()

      if (pendingNormal) {
        await pendingNormal.promise.catch(() => undefined)
      }

      if (this.generation !== generation || this.forcedFlight !== flight) {
        throw unavailable()
      }

      this.invalidate()
      flight.generation = this.generation

      return this.loadValidated(true, flight.generation)
    })()

    flight = { generation, promise }
    this.forcedFlight = flight

    void promise
      .finally(() => {
        if (this.forcedFlight === flight) {
          this.forcedFlight = null
        }
      })
      .catch(() => {})

    return promise
  }

  private startNormalFlight(generation: number): Promise<TraceCredential> {
    let flight: CredentialFlight
    const promise = this.loadValidated(false, generation)

    flight = { generation, promise }
    this.normalFlight = flight

    void promise
      .finally(() => {
        if (this.normalFlight === flight) {
          this.normalFlight = null
        }
      })
      .catch(() => {})

    return promise
  }

  private async loadValidated(forceRefresh: boolean, generation: number): Promise<TraceCredential> {
    const credential = await this.source.load(forceRefresh)
    const expiresAt = validateCredential(credential, this.clock(), this.installationId)

    if (this.generation === generation) {
      this.credential = credential
      this.credentialExpiresAt = expiresAt
    }

    return credential
  }
}

function validateCredential(credential: TraceCredential, now: number, installationId: string | undefined): number {
  const expiresAt = traceCredentialExpiresAt(credential, installationId, now)

  if (expiresAt === null) {
    throw unavailable()
  }

  return expiresAt
}

function unavailable(): Error {
  return new Error('trace_credential_unavailable')
}
