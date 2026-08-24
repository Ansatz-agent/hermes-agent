import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'

import type { TraceCredential } from './auth-bridge'
import { deriveOtlpCorrelation } from './otlp-correlation'
import { splitOtlpExportTraceRequest } from './otlp-split'
import type { TraceCredentialProvider } from './trace-credential-provider'
import type { PendingLocalCommit, TraceOutboxStoreDiagnostics } from './trace-outbox-store'
import type { DurableReceipt, DurableTraceBatch, TraceEnvelopeInput, TraceOwner } from './trace-outbox-types'
import { validateTraceOwner } from './trace-outbox-types'

export const DEFAULT_TRACE_UPSTREAM_URL = 'https://c2sml.cn/trace-ingest/v1/traces'
const MAX_LOOPBACK_BODY_BYTES = 64 * 1024 * 1024
const MAX_LOGICAL_BATCH_BYTES = 8 * 1024 * 1024
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
  | 'upload-403'

export type RecoveryTrigger = { trigger(reason: TraceRecoveryReason): void }

type TraceOutbox = {
  acknowledge(batchId: string, receipt: DurableReceipt): Promise<void>
  beginEnqueue(input: TraceEnvelopeInput): PendingLocalCommit
  diagnostics(): Promise<TraceOutboxStoreDiagnostics>
  quarantine(batchId: string, errorClass: string): Promise<void>
  quarantineInput(input: TraceEnvelopeInput, errorClass: string): Promise<DurableTraceBatch>
}

type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>

type TraceForwarderOptions = {
  credentialProvider: TraceCredentialProvider
  fetchImpl?: FetchLike
  installationId: string
  maxBodyBytes?: number
  recovery?: RecoveryTrigger
  remoteAddressForRequest?: (request: IncomingMessage) => string | undefined
  store?: TraceOutbox
  upstreamUrl?: string
}

export type TraceForwarderSummary = TraceOutboxStoreDiagnostics & { reason: 'stopped' }

type TraceMetadata = Omit<TraceEnvelopeInput, 'body' | 'contentType' | 'owner'>
type DurableOwner = { kind: 'gateway'; receipt: DurableReceipt } | { batch: DurableTraceBatch; kind: 'local' }

class UpstreamFailure extends Error {
  constructor(readonly status: number | null) {
    super(status === null ? 'trace_gateway_unavailable' : `trace_gateway_${status}`)
    this.name = 'UpstreamFailure'
  }
}

export function isExpectedTraceShutdownError(error: unknown, stopping: boolean): boolean {
  const code = (error as NodeJS.ErrnoException | null)?.code

  return stopping && (code === 'ECONNRESET' || code === 'EPIPE')
}

export class TraceForwarder {
  private readonly credentialProvider: TraceCredentialProvider
  private readonly fetchImpl: FetchLike
  private readonly installationId: string
  private readonly maxBodyBytes: number
  private readonly recovery: RecoveryTrigger
  private readonly remoteAddressForRequest: (request: IncomingMessage) => string | undefined
  private readonly store: TraceOutbox | null
  private readonly upstreamUrl: string
  private readonly upstreamControllers = new Set<AbortController>()
  private admissionRequests = 0
  private admissionTail: Promise<void> = Promise.resolve()
  private admissionOpen = false
  private localBearer = ''
  private owner: TraceOwner | null = null
  private server: http.Server | null = null
  private stopping = false

  constructor(options: TraceForwarderOptions) {
    this.credentialProvider = options.credentialProvider
    this.fetchImpl = options.fetchImpl ?? fetch
    this.installationId = options.installationId
    this.maxBodyBytes = Math.min(options.maxBodyBytes ?? MAX_LOOPBACK_BODY_BYTES, MAX_LOOPBACK_BODY_BYTES)
    this.recovery = options.recovery ?? { trigger: () => {} }
    this.remoteAddressForRequest = options.remoteAddressForRequest ?? (request => request.socket.remoteAddress)
    this.store = options.store ?? null
    this.upstreamUrl = options.upstreamUrl ?? DEFAULT_TRACE_UPSTREAM_URL
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
    this.localBearer = randomBytes(32).toString('base64url')
    this.admissionOpen = true
    this.stopping = false

    const server = http.createServer((request, response) => {
      request.on('error', error => {
        if (!isExpectedTraceShutdownError(error, this.stopping)) {
          throw error
        }
      })
      response.on('error', error => {
        if (!isExpectedTraceShutdownError(error, this.stopping)) {
          throw error
        }
      })
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

  async stop({ flushMs: _flushMs }: { flushMs: number }): Promise<TraceForwarderSummary> {
    this.stopping = true
    this.admissionOpen = false
    const server = this.server
    this.server = null
    const closed = server ? new Promise<void>(resolve => server.close(() => resolve())) : Promise.resolve()

    // `server.close()` waits for active requests. A crashed or suspended OTLP
    // producer can leave an incomplete loopback request open forever, which
    // would otherwise block terminal auth cleanup before the backend is
    // stopped. Admission is already closed, so terminate those local sockets;
    // complete batches are in the queue and still receive the bounded flush
    // below.
    server?.closeAllConnections()

    for (const controller of this.upstreamControllers) {
      controller.abort()
    }
    await closed
    this.credentialProvider.clear()
    this.localBearer = ''
    this.owner = null

    return { ...(this.store ? await this.store.diagnostics() : emptyDiagnostics()), reason: 'stopped' }
  }

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    try {
      if (!this.admissionOpen || this.owner === null || this.store === null) {
        return respond(response, 503)
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
        respond(response, isStorageFailure(error) ? 507 : 503)
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
    this.admissionRequests += 1

    return this.withAdmission(async () => {
      if (this.store === null || this.owner === null) {
        throw new Error('trace_forwarder_unavailable')
      }
      const backlog = (await this.store.diagnostics()).pending > 0
      const pending = this.store.beginEnqueue(input)

      const cloud =
        directCandidate && !backlog && validateTraceOwner(this.owner).uploadable
          ? this.sendForReceipt({ ...input, batchId: pending.batchId })
          : null

      const winner = await firstDurableOwner(pending.durable, cloud)

      if (winner.kind === 'gateway') {
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
          .then(receipt => this.store?.acknowledge(winner.batch.batchId, receipt))
          .catch(error => this.handleCloudFailure(winner.batch, error))
      }
    }).finally(() => {
      this.admissionRequests -= 1
    })
  }

  private async handleCloudFailure(batch: DurableTraceBatch, error: unknown): Promise<void> {
    if (!(error instanceof UpstreamFailure) || this.store === null) {
      return
    }

    if (error.status === 403) {
      this.recovery.trigger('upload-403')
    }

    if (error.status === 400 || error.status === 409 || error.status === 413 || error.status === 415) {
      await this.store.quarantine(batch.batchId, `gateway_${error.status}`).catch(() => undefined)
    }
  }

  private async sendForReceipt(batch: TraceEnvelopeInput & { batchId: string }): Promise<DurableReceipt> {
    try {
      const initial = await this.credentialProvider.current()
      const response = await this.fetchUpstream(batch, initial)

      if (response.status !== 401) {
        return requireGatewayReceipt(response, batch.batchId)
      }
      this.credentialProvider.invalidate()
      this.recovery.trigger('upload-401')

      return requireGatewayReceipt(
        await this.fetchUpstream(batch, await this.credentialProvider.current({ forceRefresh: true })),
        batch.batchId
      )
    } catch (error) {
      if (error instanceof UpstreamFailure) {
        throw error
      }
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

function requireGatewayReceipt(response: Response, batchId: string): DurableReceipt {
  if (response.status < 200 || response.status >= 300) {
    throw new UpstreamFailure(response.status)
  }
  const outcome = response.headers.get('x-trace-receipt')

  if (response.headers.get('x-trace-batch-id') !== batchId || (outcome !== 'accepted' && outcome !== 'duplicate')) {
    throw new UpstreamFailure(null)
  }

  return { batchId, outcome, receivedAt: Date.now() }
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

function isStorageFailure(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.message === 'storage_unavailable' || error.message === 'trace_durability_unavailable')
  )
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
