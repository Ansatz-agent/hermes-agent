import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'

import type { TraceCredential } from './auth-bridge'
import { deriveOtlpCorrelation } from './otlp-correlation'
import { splitOtlpExportTraceRequest } from './otlp-split'
import type { TraceCredentialProvider } from './trace-credential-provider'
import { respondTraceUnavailable } from './trace-ingress-facade'
import type { PendingLocalCommit, TraceOutboxStoreDiagnostics } from './trace-outbox-store'
import type { DurableReceipt, DurableTraceBatch, TraceEnvelopeInput, TraceOwner } from './trace-outbox-types'
import { isCanonicalUuidV4, validateTraceOwner } from './trace-outbox-types'
import { nextTraceRetry, parseTraceRetryAfterMs } from './trace-retry-policy'

export const DEFAULT_TRACE_UPSTREAM_URL = 'https://c2sml.cn/trace-ingest/v1/traces'
const MAX_LOOPBACK_BODY_BYTES = 64 * 1024 * 1024
const MAX_LOGICAL_BATCH_BYTES = 8 * 1024 * 1024
const LOOPBACK_DRAIN_GRACE_MS = 100
const UPSTREAM_TIMEOUT_MS = 15_000
const CORRELATION_ID = /^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/
const ENTRYPOINTS = new Set(['desktop', 'voice', 'cli', 'dashboard'])

export {
  RefreshingTraceCredentialProvider,
  type TraceCredentialProvider,
  type TraceCredentialSource
} from './trace-credential-provider'

export type TraceRecoveryReason =
  | 'enqueue'
  | 'startup'
  | 'timer'
  | 'renderer-online'
  | 'resume'
  | 'focus'
  | 'token-ready'
  | 'token-near-expiry'
  | 'upload-401'
  | 'owner-rebound'

export type RecoveryTrigger = { trigger(reason: TraceRecoveryReason): void }

type TraceOutbox = {
  acknowledge(batchId: string, receipt: DurableReceipt): Promise<void>
  beginEnqueue(input: TraceEnvelopeInput): PendingLocalCommit
  close?(): Promise<void>
  diagnostics(): Promise<TraceOutboxStoreDiagnostics>
  peekEligible(now: number): Promise<DurableTraceBatch | undefined>
  quarantine(batchId: string, errorClass: string): Promise<void>
  quarantineInput(input: TraceEnvelopeInput, errorClass: string): Promise<DurableTraceBatch>
}

type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>

type TraceForwarderOptions = {
  clock?: () => number
  credentialProvider: TraceCredentialProvider
  fetchImpl?: FetchLike
  installationId: string
  maxBodyBytes?: number
  onTerminalRevocation?: (revocation: TerminalTraceRevocation) => boolean | Promise<boolean>
  random?: () => number
  recovery?: RecoveryTrigger
  remoteAddressForRequest?: (request: IncomingMessage) => string | undefined
  store?: TraceOutbox
  upstreamUrl?: string
  uploadBarrier?: () => Promise<unknown>
}

export type TerminalTraceRevocation = {
  accountId: string
  code: 'account_disabled' | 'account_revoked' | 'session_revoked'
  revokedAt: string
  sessionId: string
}

export type TraceForwarderSummary = TraceOutboxStoreDiagnostics & { reason: 'stopped' }

type TraceMetadata = Omit<TraceEnvelopeInput, 'body' | 'contentType' | 'owner'>
type DurableOwner = { kind: 'gateway'; receipt: DurableReceipt } | { batch: DurableTraceBatch; kind: 'local' }
type UploadAuthorization = { generation: number; owner: TraceOwner }

class UpstreamFailure extends Error {
  constructor(
    readonly status: number | null,
    readonly retryAfterMs: number | null = null
  ) {
    super(status === null ? 'trace_gateway_unavailable' : `trace_gateway_${status}`)
    this.name = 'UpstreamFailure'
  }
}

export function isExpectedTraceDisconnectError(error: unknown): boolean {
  const code = (error as NodeJS.ErrnoException | null)?.code

  return code === 'ECONNRESET' || code === 'EPIPE'
}

export class TraceForwarder {
  private readonly clock: () => number
  private readonly credentialProvider: TraceCredentialProvider
  private readonly fetchImpl: FetchLike
  private readonly installationId: string
  private readonly maxBodyBytes: number
  private readonly onTerminalRevocation: (revocation: TerminalTraceRevocation) => boolean | Promise<boolean>
  private readonly random: () => number
  private readonly recovery: RecoveryTrigger
  private readonly remoteAddressForRequest: (request: IncomingMessage) => string | undefined
  private readonly store: TraceOutbox | null
  private readonly upstreamUrl: string
  private readonly uploadBarrier: (() => Promise<unknown>) | null
  private readonly upstreamControllers = new Set<AbortController>()
  private admissionRequests = 0
  private admissionTail: Promise<void> = Promise.resolve()
  private admissionOpen = false
  private authorizationGeneration = 0
  private generation = 0
  private localBearer = ''
  private owner: TraceOwner | null = null
  private recoveryPump: Promise<void> | null = null
  private readonly retryByBatch = new Map<string, { attempt: number; nextRetryAt: number }>()
  private server: http.Server | null = null
  private stopping = false
  private terminalRevoked = false

  constructor(options: TraceForwarderOptions) {
    this.clock = options.clock ?? Date.now
    this.credentialProvider = options.credentialProvider
    this.fetchImpl = options.fetchImpl ?? fetch
    this.installationId = options.installationId
    this.maxBodyBytes = Math.min(options.maxBodyBytes ?? MAX_LOOPBACK_BODY_BYTES, MAX_LOOPBACK_BODY_BYTES)
    this.onTerminalRevocation = options.onTerminalRevocation ?? (() => false)
    this.random = options.random ?? Math.random
    this.recovery = options.recovery ?? { trigger: () => {} }
    this.remoteAddressForRequest = options.remoteAddressForRequest ?? (request => request.socket.remoteAddress)
    this.store = options.store ?? null
    this.upstreamUrl = options.upstreamUrl ?? DEFAULT_TRACE_UPSTREAM_URL
    this.uploadBarrier = options.uploadBarrier ?? null
  }

  async start(owner: TraceOwner | number): Promise<{ endpoint: string; localBearer: string }> {
    if (this.server) {
      throw new Error('trace_forwarder_already_started')
    }

    if (this.store === null) {
      throw new Error('trace_outbox_required')
    }

    if (typeof owner === 'number') {
      throw new TypeError('invalid_trace_owner')
    }

    this.owner = validateTraceOwner(owner).owner
    this.generation += 1
    this.authorizationGeneration += 1
    this.terminalRevoked = false
    this.localBearer = randomBytes(32).toString('base64url')
    this.admissionOpen = true
    this.stopping = false

    const server = http.createServer((request, response) => {
      // A loopback socket error must never escape the listener: a thrown
      // error here is an uncaughtException that crashes the main process.
      request.on('error', error => this.reportLoopbackSocketError(error))
      response.on('error', error => this.reportLoopbackSocketError(error))
      void this.handle(request, response)
    })

    this.server = server
    await new Promise<void>((resolve, reject) => {
      const onError = (error: Error) => {
        server.off('listening', onListening)
        reject(error)
      }

      const onListening = () => {
        server.off('error', onError)
        resolve()
      }

      server.once('error', onError)
      server.once('listening', onListening)
      server.listen(0, '127.0.0.1')
    })
    const address = server.address()

    if (!address || typeof address === 'string') {
      await this.stop({ flushMs: 0 })
      throw new Error('trace_forwarder_unavailable')
    }

    return { endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: this.localBearer }
  }

  ingress(): { endpoint: string; localBearer: string } | null {
    const address = this.server?.address()

    return address && typeof address !== 'string' && this.localBearer
      ? { endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: this.localBearer }
      : null
  }

  rebindOwner(owner: TraceOwner): void {
    const next = validateTraceOwner(owner).owner
    const current = this.owner

    if (!this.admissionOpen || current === null) {
      throw new Error('trace_forwarder_unavailable')
    }

    if (current.accountKey !== next.accountKey || current.installationId !== next.installationId) {
      throw new Error('trace_owner_account_mismatch')
    }

    this.authorizationGeneration += 1
    this.owner = { ...next }
    this.credentialProvider.invalidate()
    this.retryByBatch.clear()
    this.recovery.trigger('owner-rebound')
  }

  async stop({ flushMs: _flushMs }: { flushMs: number }): Promise<TraceForwarderSummary> {
    this.stopping = true
    this.admissionOpen = false
    this.generation += 1
    this.authorizationGeneration += 1
    const server = this.server
    this.server = null
    const closed = server ? new Promise<void>(resolve => server.close(() => resolve())) : Promise.resolve()

    // Let a body that has already reached durable admission finish and receive
    // its acknowledgement. A crashed or suspended producer can still hold an
    // incomplete body forever, so force-close any remaining local sockets
    // after a short bounded drain window.
    const forceCloseTimer = server ? setTimeout(() => server.closeAllConnections(), LOOPBACK_DRAIN_GRACE_MS) : null
    forceCloseTimer?.unref?.()

    for (const controller of this.upstreamControllers) {
      controller.abort()
    }

    await closed

    if (forceCloseTimer) {
      clearTimeout(forceCloseTimer)
    }

    await this.admissionTail
    await this.store?.close?.()
    this.credentialProvider.clear()
    this.localBearer = ''
    this.owner = null

    return { ...(this.store ? await this.store.diagnostics() : emptyDiagnostics()), reason: 'stopped' }
  }

  pump(): Promise<void> {
    if (this.recoveryPump !== null) {
      return this.recoveryPump
    }

    const current = this.pumpUntilBlocked()
    this.recoveryPump = current

    void current
      .finally(() => {
        if (this.recoveryPump === current) {
          this.recoveryPump = null
        }
      })
      .catch(() => {})

    return current
  }

  nextRecoveryAt(): number | null {
    let earliest: number | null = null

    for (const retry of this.retryByBatch.values()) {
      earliest = earliest === null ? retry.nextRetryAt : Math.min(earliest, retry.nextRetryAt)
    }

    return earliest
  }

  private reportLoopbackSocketError(error: unknown): void {
    if (!isExpectedTraceDisconnectError(error)) {
      console.error('[trace-forwarder] unexpected loopback socket error', error)
    }
  }

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    try {
      if (!this.admissionOpen || this.owner === null || this.store === null) {
        return respondTraceUnavailable(response)
      }

      if (!isLoopback(this.remoteAddressForRequest(request))) {
        return respond(response, 403)
      }

      if (request.method !== 'POST' || request.url !== '/v1/traces') {
        return respond(response, 405)
      }

      if (!matchesBearer(request.headers.authorization, this.localBearer)) {
        return respond(response, 401)
      }

      if (request.headers['content-type'] !== 'application/x-protobuf') {
        return respond(response, 415)
      }

      if (request.headers['content-encoding'] !== undefined && request.headers['content-encoding'] !== 'identity') {
        return respond(response, 415)
      }

      const length = Number(request.headers['content-length'])

      if (Number.isFinite(length) && length > this.maxBodyBytes) {
        request.resume()

        return respond(response, 413)
      }

      const body = await readBoundedBody(request, this.maxBodyBytes)

      if (!body) {
        return respond(response, 413)
      }

      const metadata = traceMetadata(request, body)

      if (!metadata) {
        return respond(response, 400)
      }

      const split =
        body.length <= MAX_LOGICAL_BATCH_BYTES
          ? { batches: [body], oversizedSpans: [], parts: [{ body, kind: 'batch' as const }] }
          : splitOtlpExportTraceRequest(body, MAX_LOGICAL_BATCH_BYTES)

      let rejectedOversize = false

      for (const part of split.parts) {
        if (part.kind === 'oversized-span') {
          await this.store.quarantineInput(this.envelope(metadata, part.body), 'payload_too_large')
          rejectedOversize = true
        } else {
          await this.admitBatch(this.envelope(metadata, part.body))
        }
      }

      respond(response, rejectedOversize ? 413 : 200, !rejectedOversize)
    } catch (error) {
      if (!response.headersSent) {
        respondTraceUnavailable(response)
      } else {
        response.end()
      }
    }
  }

  private envelope(metadata: TraceMetadata, body: Buffer): TraceEnvelopeInput {
    if (this.owner === null) {
      throw new Error('trace_forwarder_unavailable')
    }

    return { ...metadata, body, contentType: 'application/x-protobuf', owner: this.owner }
  }

  private admitBatch(input: TraceEnvelopeInput): Promise<void> {
    const directCandidate = this.admissionRequests === 0
    const generation = this.generation
    this.admissionRequests += 1

    return this.withAdmission(async () => {
      if (this.store === null || this.owner === null) {
        throw new Error('trace_forwarder_unavailable')
      }

      const backlog = (await this.store.diagnostics()).pending > 0
      this.requireActiveOwner(input.owner, generation)
      const pending = this.store.beginEnqueue(input)
      const authorization = this.captureUploadAuthorization()

      const cloud =
        directCandidate && this.uploadBarrier === null && !backlog && validateTraceOwner(this.owner).uploadable
          ? this.sendForReceipt({ ...input, batchId: pending.batchId }, generation, authorization, false)
          : null

      const winner = await firstDurableOwner(pending.durable, cloud)

      if (winner.kind === 'gateway') {
        this.requireActiveOwner(input.owner, generation)

        try {
          this.requireUploadAuthorization(authorization.owner, authorization.generation)
        } catch {
          await pending.durable
          this.recovery.trigger('enqueue')

          return
        }

        void pending.durable.catch(() => undefined)

        try {
          await pending.cancelForGatewayReceipt(winner.receipt)
        } catch {
          throw new Error('trace_durability_unavailable')
        }

        return
      }

      this.recovery.trigger('enqueue')

      if (cloud !== null) {
        void cloud
          .then(async receipt => {
            this.requireActiveOwner(winner.batch.owner, generation)
            this.requireUploadAuthorization(authorization.owner, authorization.generation)
            await this.store?.acknowledge(winner.batch.batchId, receipt)
          })
          .catch(error => this.handleCloudFailure(winner.batch, error, generation, authorization.generation))
      }
    }).finally(() => {
      this.admissionRequests -= 1
    })
  }

  private async handleCloudFailure(
    batch: DurableTraceBatch,
    error: unknown,
    generation: number,
    authorizationGeneration: number
  ): Promise<void> {
    if (!(error instanceof UpstreamFailure) || this.store === null) {
      return
    }

    if (authorizationGeneration !== this.authorizationGeneration) {
      return
    }

    try {
      this.requireActiveOwner(batch.owner, generation)
    } catch {
      return
    }

    if (error.status === 403 && !this.terminalRevoked) {
      this.deferRetry(batch.batchId, error.retryAfterMs, 1_000)
    }

    if (error.status === 400 || error.status === 409 || error.status === 413 || error.status === 415) {
      await this.store.quarantine(batch.batchId, `gateway_${error.status}`).catch(() => undefined)
    }
  }

  private async pumpUntilBlocked(): Promise<void> {
    if (!this.admissionOpen || this.owner === null || this.store === null) {
      return
    }

    if (!validateTraceOwner(this.owner).uploadable || this.terminalRevoked) {
      return
    }

    const owner = this.owner
    const generation = this.generation
    await this.uploadBarrier?.()
    this.requireActiveOwner(owner, generation)

    while (this.admissionOpen && this.owner !== null && this.store !== null) {
      const now = this.clock()
      const batch = await this.store.peekEligible(now)
      this.requireActiveOwner(owner, generation)

      if (batch === undefined) {
        this.retryByBatch.clear()

        return
      }

      for (const batchId of this.retryByBatch.keys()) {
        if (batchId !== batch.batchId) {
          this.retryByBatch.delete(batchId)
        }
      }

      const retry = this.retryByBatch.get(batch.batchId)

      if (retry !== undefined && retry.nextRetryAt > now) {
        return
      }

      const authorization = this.captureUploadAuthorization()

      try {
        const receipt = await this.sendForReceipt(batch, generation, authorization, true)
        this.requireActiveOwner(owner, generation)
        this.requireUploadAuthorization(authorization.owner, authorization.generation)
        await this.store.acknowledge(batch.batchId, receipt)
        this.retryByBatch.delete(batch.batchId)
      } catch (error) {
        if (authorization.generation !== this.authorizationGeneration) {
          return
        }

        if (!(error instanceof UpstreamFailure)) {
          this.deferRetry(batch.batchId, null)

          return
        }

        if (error.status === 403) {
          if (!this.terminalRevoked) {
            this.deferRetry(batch.batchId, error.retryAfterMs, 1_000)
          }

          return
        }

        if (error.status === 400 || error.status === 409 || error.status === 413 || error.status === 415) {
          this.requireActiveOwner(owner, generation)
          await this.store.quarantine(batch.batchId, `gateway_${error.status}`)
          this.retryByBatch.delete(batch.batchId)

          continue
        }

        this.deferRetry(batch.batchId, error.retryAfterMs)

        return
      }
    }
  }

  private deferRetry(batchId: string, retryAfterMs: number | null, minimumDelayMs = 0): void {
    const previous = this.retryByBatch.get(batchId)
    const attempt = previous === undefined ? 0 : previous.attempt + 1
    const now = this.clock()

    const nextRetryAt = Math.max(
      nextTraceRetry({ attempt, now, random: this.random, retryAfterMs }),
      now + minimumDelayMs
    )

    this.retryByBatch.set(batchId, { attempt, nextRetryAt })
  }

  private async sendForReceipt(
    batch: TraceEnvelopeInput & { batchId: string },
    generation: number,
    authorization: UploadAuthorization,
    awaitTerminalRevocation: boolean
  ): Promise<DurableReceipt> {
    try {
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)
      await this.uploadBarrier?.()
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)
      const initial = await this.credentialProvider.current()
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)
      const response = await this.fetchUpstream(batch, initial)
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)

      if (response.status !== 401) {
        return await this.requireGatewayReceipt(
          response,
          batch,
          generation,
          authorization,
          awaitTerminalRevocation
        )
      }

      this.credentialProvider.invalidate()
      this.recovery.trigger('upload-401')
      const refreshed = await this.credentialProvider.current({ forceRefresh: true })
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)
      this.recovery.trigger('token-ready')

      const retried = await this.fetchUpstream(batch, refreshed)
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)

      return await this.requireGatewayReceipt(retried, batch, generation, authorization, awaitTerminalRevocation)
    } catch (error) {
      if (error instanceof UpstreamFailure) {
        throw error
      }

      throw new UpstreamFailure(null)
    }
  }

  private async requireGatewayReceipt(
    response: Response,
    batch: TraceEnvelopeInput & { batchId: string },
    generation: number,
    authorization: UploadAuthorization,
    awaitTerminalRevocation: boolean
  ): Promise<DurableReceipt> {
    if (response.status === 403) {
      const revocation = await parseTerminalRevocation(response)
      this.requireActiveOwner(batch.owner, generation)
      this.requireUploadAuthorization(authorization.owner, authorization.generation)

      if (
        !this.terminalRevoked &&
        revocation !== null &&
        revocation.accountId === authorization.owner.accountId &&
        revocation.sessionId === authorization.owner.sessionId
      ) {
        const confirmation = this.confirmTerminalRevocation(revocation, batch.owner, generation, authorization)

        if (awaitTerminalRevocation) {
          await confirmation
        } else {
          // Direct upload runs inside the local durability admission lock.
          // Detaching avoids a callback -> stop() -> admissionTail cycle;
          // the local/cloud durability race can settle before stop waits.
          void confirmation.catch(() => {})
        }
      }
    }

    return requireGatewayReceipt(response, batch.batchId, this.clock())
  }

  private async confirmTerminalRevocation(
    revocation: TerminalTraceRevocation,
    batchOwner: TraceOwner,
    generation: number,
    authorization: UploadAuthorization
  ): Promise<void> {
    let confirmed = false

    try {
      confirmed = await this.onTerminalRevocation(revocation)
    } catch {
      // An unconfirmed revocation remains retryable. Durable outbox state
      // is deliberately isolated from callback failures.
    }

    this.requireActiveOwner(batchOwner, generation)
    this.requireUploadAuthorization(authorization.owner, authorization.generation)

    if (confirmed) {
      this.terminalRevoked = true
      this.retryByBatch.clear()
    }
  }

  private requireActiveOwner(owner: TraceOwner, generation: number): void {
    if (!this.admissionOpen || this.generation !== generation || this.owner?.accountKey !== owner.accountKey) {
      throw new UpstreamFailure(null)
    }
  }

  private captureUploadAuthorization(): UploadAuthorization {
    if (this.owner === null) {
      throw new UpstreamFailure(null)
    }

    return { generation: this.authorizationGeneration, owner: { ...this.owner } }
  }

  private requireUploadAuthorization(owner: TraceOwner, generation: number): void {
    if (
      generation !== this.authorizationGeneration ||
      this.owner?.accountKey !== owner.accountKey ||
      this.owner?.sessionId !== owner.sessionId
    ) {
      throw new UpstreamFailure(null)
    }
  }

  private async fetchUpstream(
    batch: TraceEnvelopeInput & { batchId: string },
    credential: TraceCredential
  ): Promise<Response> {
    if (credential.installation_id !== this.installationId) {
      throw new Error('trace_credential_unavailable')
    }

    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
    this.upstreamControllers.add(controller)

    try {
      return await this.fetchImpl(this.upstreamUrl, {
        method: 'POST',
        body: batch.body as unknown as BodyInit,
        headers: {
          authorization: `Bearer ${credential.access_token}`,
          'content-type': batch.contentType,
          'idempotency-key': batch.batchId,
          'x-hermes-session-id': batch.hermesSessionId,
          'x-telemetry-schema-version': batch.telemetrySchemaVersion,
          'x-trace-entrypoint': batch.entrypoint,
          'x-trace-payload-sha256': createHash('sha256').update(batch.body).digest('hex'),
          'x-trace-run-id': batch.runId
        },
        signal: controller.signal
      })
    } finally {
      clearTimeout(timeout)
      this.upstreamControllers.delete(controller)
    }
  }

  private async withAdmission<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.admissionTail
    let release!: () => void
    this.admissionTail = new Promise<void>(resolve => {
      release = resolve
    })
    await previous

    try {
      return await operation()
    } finally {
      release()
    }
  }
}

function traceMetadata(request: IncomingMessage, body: Buffer): TraceMetadata | null {
  const hermesSessionId = singleHeader(request.headers['x-hermes-session-id'])
  const entrypoint = singleHeader(request.headers['x-trace-entrypoint'])
  const runId = singleHeader(request.headers['x-trace-run-id'])
  const telemetrySchemaVersion = singleHeader(request.headers['x-telemetry-schema-version'])
  const supplied = [hermesSessionId, entrypoint, runId, telemetrySchemaVersion].filter(Boolean).length

  if (supplied === 0) {
    const derived = deriveOtlpCorrelation(body)

    return derived
      ? {
          entrypoint: 'desktop',
          hermesSessionId: derived.sessionId,
          runId: derived.runId,
          telemetrySchemaVersion: '1'
        }
      : null
  }

  if (
    supplied !== 4 ||
    !hermesSessionId ||
    !CORRELATION_ID.test(hermesSessionId) ||
    !entrypoint ||
    !ENTRYPOINTS.has(entrypoint) ||
    !runId ||
    !CORRELATION_ID.test(runId) ||
    telemetrySchemaVersion !== '1'
  ) {
    return null
  }

  return { entrypoint: entrypoint as TraceMetadata['entrypoint'], hermesSessionId, runId, telemetrySchemaVersion }
}

async function firstDurableOwner(
  local: Promise<DurableTraceBatch>,
  cloud: Promise<DurableReceipt> | null
): Promise<DurableOwner> {
  if (cloud === null) {
    return { batch: await local, kind: 'local' }
  }

  return new Promise<DurableOwner>((resolve, reject) => {
    let localFailed = false
    let cloudFailed = false

    const failed = () => {
      if (localFailed && cloudFailed) {
        reject(new Error('trace_durability_unavailable'))
      }
    }

    void local.then(
      batch => resolve({ batch, kind: 'local' }),
      () => {
        localFailed = true
        failed()
      }
    )
    void cloud.then(
      receipt => resolve({ kind: 'gateway', receipt }),
      () => {
        cloudFailed = true
        failed()
      }
    )
  })
}

function requireGatewayReceipt(response: Response, batchId: string, now: number): DurableReceipt {
  if (response.status < 200 || response.status >= 300) {
    throw new UpstreamFailure(response.status, parseTraceRetryAfterMs(response.headers.get('retry-after'), now))
  }

  const outcome = response.headers.get('x-trace-receipt')

  if (response.headers.get('x-trace-batch-id') !== batchId || (outcome !== 'accepted' && outcome !== 'duplicate')) {
    throw new UpstreamFailure(null)
  }

  return { batchId, outcome, receivedAt: now }
}

const TERMINAL_REVOCATION_CODES = new Set<TerminalTraceRevocation['code']>([
  'account_disabled',
  'account_revoked',
  'session_revoked'
])

const RFC3339_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

async function parseTerminalRevocation(response: Response): Promise<TerminalTraceRevocation | null> {
  const contentLength = Number(response.headers.get('content-length'))

  if (Number.isFinite(contentLength) && contentLength > 4_096) {
    return null
  }

  let value: unknown

  try {
    const text = await response.text()

    if (Buffer.byteLength(text) > 4_096) {
      return null
    }

    value = JSON.parse(text)
  } catch {
    return null
  }

  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const body = value as Record<string, unknown>

  if (
    Object.keys(body).sort().join(',') !== 'account_id,code,retryable,revoked_at,session_id,state' ||
    body.state !== 'revoked' ||
    body.retryable !== false ||
    !isCanonicalUuidV4(body.account_id) ||
    !isCanonicalUuidV4(body.session_id) ||
    typeof body.code !== 'string' ||
    !TERMINAL_REVOCATION_CODES.has(body.code as TerminalTraceRevocation['code']) ||
    typeof body.revoked_at !== 'string' ||
    !RFC3339_TIMESTAMP.test(body.revoked_at) ||
    !Number.isFinite(Date.parse(body.revoked_at))
  ) {
    return null
  }

  return {
    accountId: body.account_id,
    code: body.code as TerminalTraceRevocation['code'],
    revokedAt: body.revoked_at,
    sessionId: body.session_id
  }
}

function singleHeader(value: string | string[] | undefined): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function isLoopback(address: string | undefined): boolean {
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1'
}

function matchesBearer(authorization: string | undefined, bearer: string): boolean {
  if (!authorization || !bearer) {
    return false
  }

  const expected = Buffer.from(`Bearer ${bearer}`)
  const actual = Buffer.from(authorization)

  return actual.length === expected.length && timingSafeEqual(actual, expected)
}

async function readBoundedBody(request: IncomingMessage, maxBytes: number): Promise<Buffer | null> {
  const chunks: Buffer[] = []
  let size = 0

  for await (const chunk of request) {
    const bytes = Buffer.from(chunk)
    size += bytes.length

    if (size > maxBytes) {
      request.resume()

      return null
    }

    chunks.push(bytes)
  }

  return Buffer.concat(chunks, size)
}

function emptyDiagnostics(): TraceOutboxStoreDiagnostics {
  return {
    accepted: 0,
    deduplicated: 0,
    duplicate: 0,
    evictedCapacity: 0,
    expired: 0,
    keyLost: 0,
    payloadBytes: 0,
    pending: 0,
    pendingBytes: 0,
    quarantined: 0,
    recoveredCorruptTail: 0,
    tombstoneBytes: 0,
    tombstones: 0
  }
}

function respond(response: ServerResponse, status: number, protobuf = false): void {
  response.writeHead(status, {
    'cache-control': 'no-store',
    'content-type': protobuf ? 'application/x-protobuf' : 'application/json',
    'content-length': '0'
  })
  response.end()
}
