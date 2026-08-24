import { randomBytes, timingSafeEqual } from 'node:crypto'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'

import type { TraceCredential } from './auth-bridge'
import { deriveOtlpCorrelation } from './otlp-correlation'
import type { TraceCredentialProvider } from './trace-credential-provider'
import {
  TraceForwarderQueue,
  type TraceForwarderQueueSummary,
  type TraceQueueBatch,
  type TraceQueueSendResult
} from './trace-forwarder-queue'

export const DEFAULT_TRACE_UPSTREAM_URL = 'https://c2sml.cn/trace-ingest/v1/traces'
const MAX_BODY_BYTES = 8 * 1024 * 1024
const UPSTREAM_TIMEOUT_MS = 15_000
const CORRELATION_ID = /^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/
const ENTRYPOINTS = new Set(['desktop', 'voice', 'cli', 'dashboard'])

export {
  RefreshingTraceCredentialProvider,
  type TraceCredentialProvider,
  type TraceCredentialSource
} from './trace-credential-provider'

type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>

type TraceForwarderOptions = {
  credentialProvider: TraceCredentialProvider
  fetchImpl?: FetchLike
  installationId: string
  maxBodyBytes?: number
  queue?: TraceForwarderQueue
  remoteAddressForRequest?: (request: IncomingMessage) => string | undefined
  upstreamUrl?: string
}

export type TraceForwarderSummary = TraceForwarderQueueSummary & {
  reason: 'stopped'
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
  private readonly queue: TraceForwarderQueue
  private readonly remoteAddressForRequest: (request: IncomingMessage) => string | undefined
  private readonly upstreamUrl: string
  private activeEpoch: number | null = null
  private admissionOpen = false
  private inFlightController: AbortController | null = null
  private localBearer = ''
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private server: http.Server | null = null
  private stopping = false

  constructor(options: TraceForwarderOptions) {
    this.credentialProvider = options.credentialProvider
    this.fetchImpl = options.fetchImpl ?? fetch
    this.installationId = options.installationId
    this.maxBodyBytes = options.maxBodyBytes ?? MAX_BODY_BYTES
    this.queue = options.queue ?? new TraceForwarderQueue()
    this.remoteAddressForRequest = options.remoteAddressForRequest ?? (request => request.socket.remoteAddress)
    this.upstreamUrl = options.upstreamUrl ?? DEFAULT_TRACE_UPSTREAM_URL
  }

  async start(epoch: number): Promise<{ endpoint: string; localBearer: string }> {
    if (this.server) {
      throw new Error('trace_forwarder_already_started')
    }

    this.queue.activateEpoch(epoch)
    this.activeEpoch = epoch
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
      void this.handle(request, response, epoch)
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

    return {
      endpoint: `http://127.0.0.1:${address.port}/v1/traces`,
      localBearer: this.localBearer
    }
  }

  async stop({ flushMs }: { flushMs: number }): Promise<TraceForwarderSummary> {
    this.stopping = true
    this.admissionOpen = false
    this.clearRetryTimer()
    const server = this.server

    this.server = null

    const closePromise = server ? new Promise<void>(resolve => server.close(() => resolve())) : Promise.resolve()
    // `server.close()` waits for active requests. A crashed or suspended OTLP
    // producer can leave an incomplete loopback request open forever, which
    // would otherwise block terminal auth cleanup before the backend is
    // stopped. Admission is already closed, so terminate those local sockets;
    // complete batches are in the queue and still receive the bounded flush
    // below.
    server?.closeAllConnections()

    const deadline = Date.now() + Math.max(0, flushMs)

    while (this.queue.summary().queued > 0 && Date.now() < deadline) {
      await this.queue.pump(batch => this.sendBatch(batch), { ignoreBackoff: true })

      if (this.queue.summary().queued > 0) {
        await new Promise(resolve => setTimeout(resolve, Math.min(10, Math.max(0, deadline - Date.now()))))
      }
    }

    if (this.inFlightController) {
      this.inFlightController.abort()
      this.inFlightController = null
    }

    this.queue.clear()
    this.credentialProvider.clear()
    this.localBearer = ''
    this.activeEpoch = null
    await closePromise

    return { ...this.queue.summary(), reason: 'stopped' }
  }

  private async handle(request: IncomingMessage, response: ServerResponse, epoch: number): Promise<void> {
    try {
      if (!this.admissionOpen || epoch !== this.activeEpoch) {
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

      const contentEncoding = request.headers['content-encoding']

      if (contentEncoding !== undefined && contentEncoding !== 'identity') {
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

      const accepted = this.queue.enqueue({
        ...metadata,
        body,
        contentType: 'application/x-protobuf',
        epoch
      })

      if (!accepted.accepted) {
        return respond(response, 503)
      }

      respond(response, 200, true)
      this.kick()
    } catch {
      if (!response.headersSent) {
        respond(response, 503)
      } else {
        response.end()
      }
    }
  }

  private kick(): void {
    void this.queue
      .pump(batch => this.sendBatch(batch))
      .catch(() => {})
      .finally(() => this.scheduleRetry())
  }

  private scheduleRetry(): void {
    this.clearRetryTimer()

    if (!this.admissionOpen) {
      return
    }

    const retryAt = this.queue.nextRetryAt()

    if (retryAt === null) {
      return
    }

    this.retryTimer = setTimeout(
      () => {
        this.retryTimer = null
        this.kick()
      },
      Math.max(0, retryAt - Date.now())
    )
  }

  private clearRetryTimer(): void {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }

  private async sendBatch(batch: TraceQueueBatch): Promise<TraceQueueSendResult> {
    if (!this.admissionOpen || batch.epoch !== this.activeEpoch) {
      return 'drop'
    }

    try {
      const initial = await this.credentialProvider.current()
      const response = await this.fetchUpstream(batch, initial)

      if (response.status === 401) {
        this.credentialProvider.invalidate()
        const rotated = await this.credentialProvider.current({ forceRefresh: true })
        const retried = await this.fetchUpstream(batch, rotated)

        return classifyUpstream(retried.status)
      }

      return classifyUpstream(response.status)
    } catch {
      return 'retry'
    }
  }

  private async fetchUpstream(batch: TraceQueueBatch, credential: TraceCredential): Promise<Response> {
    if (credential.installation_id !== this.installationId) {
      throw new Error('trace_credential_unavailable')
    }

    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)

    this.inFlightController = controller

    try {
      return await this.fetchImpl(this.upstreamUrl, {
        method: 'POST',
        body: batch.body as unknown as BodyInit,
        headers: {
          authorization: `Bearer ${credential.access_token}`,
          'content-type': batch.contentType,
          'x-hermes-session-id': batch.sessionId,
          'x-telemetry-schema-version': batch.telemetrySchemaVersion,
          'x-trace-entrypoint': batch.entrypoint,
          'x-trace-run-id': batch.runId
        },
        signal: controller.signal
      })
    } finally {
      clearTimeout(timeout)

      if (this.inFlightController === controller) {
        this.inFlightController = null
      }
    }
  }
}

function traceMetadata(
  request: IncomingMessage,
  body: Buffer
): Omit<TraceQueueBatch, 'body' | 'contentType' | 'epoch'> | null {
  const sessionId = singleHeader(request.headers['x-hermes-session-id'])
  const entrypoint = singleHeader(request.headers['x-trace-entrypoint'])
  const runId = singleHeader(request.headers['x-trace-run-id'])
  const telemetrySchemaVersion = singleHeader(request.headers['x-telemetry-schema-version'])
  const supplied = [sessionId, entrypoint, runId, telemetrySchemaVersion].filter(Boolean).length

  if (supplied === 0) {
    const derived = deriveOtlpCorrelation(body)

    return derived
      ? {
          ...derived,
          entrypoint: 'desktop',
          telemetrySchemaVersion: '1'
        }
      : null
  }

  if (
    supplied !== 4 ||
    !sessionId ||
    !CORRELATION_ID.test(sessionId) ||
    !entrypoint ||
    !ENTRYPOINTS.has(entrypoint) ||
    !runId ||
    !CORRELATION_ID.test(runId) ||
    telemetrySchemaVersion !== '1'
  ) {
    return null
  }

  return {
    entrypoint: entrypoint as TraceQueueBatch['entrypoint'],
    runId,
    sessionId,
    telemetrySchemaVersion
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

function classifyUpstream(status: number): TraceQueueSendResult {
  if (status >= 200 && status < 300) {
    return 'sent'
  }

  if (status === 400 || status === 413 || status === 415) {
    return 'drop'
  }

  return 'retry'
}

function respond(response: ServerResponse, status: number, protobuf = false): void {
  response.writeHead(status, {
    'cache-control': 'no-store',
    'content-type': protobuf ? 'application/x-protobuf' : 'application/json',
    'content-length': '0'
  })
  response.end()
}
