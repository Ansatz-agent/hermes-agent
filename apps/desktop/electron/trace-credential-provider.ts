import type { TraceCredential } from './auth-bridge'

const EXPIRY_SKEW_MS = 60_000
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

export type TraceCredentialSource = {
  load(forceRefresh: boolean): Promise<TraceCredential>
}

export type TraceCredentialProvider = {
  current(options?: { forceRefresh?: boolean }): Promise<TraceCredential>
  invalidate(): void
  clear(): void
  expiresAt(): number | null
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
  if (typeof credential !== 'object' || credential === null) {
    throw unavailable()
  }

  const expiresAt = Date.parse(credential.expires_at)

  if (
    typeof credential.access_token !== 'string' ||
    credential.access_token.length < 20 ||
    credential.access_token.length > 4_096 ||
    /[\r\n]/.test(credential.access_token) ||
    typeof credential.expires_at !== 'string' ||
    credential.expires_at.length > 128 ||
    !RFC3339.test(credential.expires_at) ||
    !Number.isSafeInteger(expiresAt) ||
    expiresAt <= now ||
    !Number.isSafeInteger(credential.expires_in) ||
    credential.expires_in < 1 ||
    credential.expires_in > 900 ||
    typeof credential.installation_id !== 'string' ||
    !UUID_V4.test(credential.installation_id) ||
    (installationId !== undefined && credential.installation_id !== installationId)
  ) {
    throw unavailable()
  }

  return expiresAt
}

function unavailable(): Error {
  return new Error('trace_credential_unavailable')
}
