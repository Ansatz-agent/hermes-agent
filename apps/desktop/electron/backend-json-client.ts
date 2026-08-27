import http from 'node:http'
import https from 'node:https'

import type {
  LocalCapabilitySnapshot,
  RotationReason
} from './local-capability-manager'
import { redactSecrets } from './ssh-connection'

const DEFAULT_TIMEOUT_MS = 20_000
const MAX_RECOVERY_WAIT_MS = 20_000
const MAX_BODY_PREVIEW_CHARS = 200
const SENSITIVE_BODY_KEY = /^(?:authorization|bearer|cookie|csrf|password|secret|client[_-]?secret|(?:x[_-]?hermes[_-]?)?session[_-]?token|(?:access|refresh|id)[_-]?token|api[_-]?key|token)$/i
const SENSITIVE_BODY_ASSIGNMENT = /(["']?(?:authorization|bearer|cookie|csrf|password|secret|client[_-]?secret|(?:x[_-]?hermes[_-]?)?session[_-]?token|(?:access|refresh|id)[_-]?token|api[_-]?key|token)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,\r\n}]+)/gi

type LocalCapabilitySource = {
  snapshot: (key: string) => LocalCapabilitySnapshot
  refresh: (key: string, reason: RotationReason) => Promise<LocalCapabilitySnapshot>
}

export type BackendJsonTransportRequest = {
  url: string
  token: string
  method: string
  bodyBytes: Buffer | undefined
  timeoutMs: number
  bearer?: string | null
}

export type BackendJsonTransport<T = unknown> = (
  request: BackendJsonTransportRequest
) => Promise<T>

export type LocalCapabilityJsonRequest<T = unknown> = {
  manager: LocalCapabilitySource
  key: string
  url: string
  method?: string
  body?: unknown
  timeoutMs?: number
  transport?: BackendJsonTransport<T>
}

function safeString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function safeBody(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function redactStructuredBody(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(redactStructuredBody)
  }

  if (!value || typeof value !== 'object') {
    return value
  }

  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, entry]) => [
      key,
      SENSITIVE_BODY_KEY.test(key) ? '<redacted>' : redactStructuredBody(entry)
    ])
  )
}

function redactBodyPreview(value: string): string {
  let preview = value
  let parsed = false

  try {
    preview = JSON.stringify(redactStructuredBody(JSON.parse(value)))
    parsed = true
  } catch {
    // Preserve non-JSON provider error text; the assignment scrub below is
    // deliberately tolerant of malformed bodies.
  }

  const sanitized = redactSecrets(preview)

  return (parsed
    ? sanitized
    : sanitized.replace(SENSITIVE_BODY_ASSIGNMENT, '$1<redacted>')
  ).slice(0, MAX_BODY_PREVIEW_CHARS)
}

export class BackendHttpError extends Error {
  readonly status: number
  readonly code: string | null
  readonly reason: string | null
  readonly failurePhase: string | null
  readonly retryable: boolean
  readonly bodyPreview: string

  constructor(status: number, body: Record<string, unknown>, bodyPreview = '') {
    const sanitizedPreview = redactBodyPreview(bodyPreview)

    super(
      sanitizedPreview
        ? `${status}: ${sanitizedPreview}`
        : `Backend request failed (${status})`
    )
    this.name = 'Error'
    this.status = status
    this.code = safeString(body.code)
    this.reason = safeString(body.reason)
    this.failurePhase = safeString(body.failure_phase)
    this.retryable = body.retryable === true
    this.bodyPreview = sanitizedPreview
  }
}

export function isRetryableLocalCapabilityError(
  error: unknown
): error is BackendHttpError {
  return (
    error instanceof BackendHttpError &&
    error.status === 401 &&
    error.code === 'local_capability_rejected' &&
    error.failurePhase === 'pre_dispatch' &&
    error.retryable === true
  )
}

function normalizedTimeout(value: number | undefined): number {
  if (value === undefined) {
    return DEFAULT_TIMEOUT_MS
  }

  if (!Number.isFinite(value) || value <= 0) {
    throw new TypeError('Invalid backend request timeout')
  }

  return Math.round(value)
}

function sameRequestAuthority(
  first: LocalCapabilitySnapshot,
  replacement: LocalCapabilitySnapshot,
  key: string
): boolean {
  return (
    replacement.key === key &&
    replacement.registrationId !== first.registrationId &&
    replacement.backendGeneration === first.backendGeneration &&
    replacement.scope.connection_id === first.scope.connection_id &&
    replacement.scope.runtime_instance_id === first.scope.runtime_instance_id &&
    replacement.scope.epoch === first.scope.epoch
  )
}

async function refreshBeforeDeadline(
  manager: LocalCapabilitySource,
  key: string,
  timeoutMs: number,
  originalError: BackendHttpError
): Promise<LocalCapabilitySnapshot> {
  const waitMs = Math.min(timeoutMs, MAX_RECOVERY_WAIT_MS)
  let timer: ReturnType<typeof setTimeout> | undefined

  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(originalError), waitMs)
    timer.unref?.()
  })

  const refresh = Promise.resolve()
    .then(() => manager.refresh(key, 'recovery'))
    .catch(() => {
      throw originalError
    })

  try {
    return await Promise.race([refresh, deadline])
  } finally {
    if (timer) {
      clearTimeout(timer)
    }
  }
}

export async function requestJsonWithLocalCapability<T = unknown>(
  options: LocalCapabilityJsonRequest<T>
): Promise<T> {
  const method = String(options.method || 'GET').toUpperCase()

  const bodyBytes =
    options.body === undefined
      ? undefined
      : Buffer.from(JSON.stringify(options.body), 'utf8')

  const timeoutMs = normalizedTimeout(options.timeoutMs)

  const transport: BackendJsonTransport<T> =
    options.transport ?? backendJsonTransport<T>

  const first = options.manager.snapshot(options.key)

  const dispatch = (capability: LocalCapabilitySnapshot) =>
    transport({
      url: options.url,
      token: capability.bearer,
      method,
      bodyBytes,
      timeoutMs
    })

  try {
    return await dispatch(first)
  } catch (error) {
    if (!isRetryableLocalCapabilityError(error)) {
      throw error
    }

    const replacement = await refreshBeforeDeadline(
      options.manager,
      options.key,
      timeoutMs,
      error
    )

    if (!sameRequestAuthority(first, replacement, options.key)) {
      throw error
    }

    return dispatch(replacement)
  }
}

export function backendJsonTransport<T = unknown>(
  request: BackendJsonTransportRequest
): Promise<T> {
  return new Promise((resolve, reject) => {
    let parsed: URL

    try {
      parsed = new URL(request.url)
    } catch {
      reject(new Error('Invalid Hermes backend URL'))

      return
    }

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

      return
    }

    const client = parsed.protocol === 'https:' ? https : http

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'X-Hermes-Session-Token': request.token
    }

    if (request.bearer) {
      headers.Authorization = `Bearer ${request.bearer}`
    }

    if (request.bodyBytes) {
      headers['Content-Length'] = String(request.bodyBytes.length)
    }

    const outgoing = client.request(
      parsed,
      { method: request.method, headers },
      response => {
        const chunks: Buffer[] = []

        response.on('error', reject)
        response.on('data', chunk => chunks.push(Buffer.from(chunk)))
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8')
          const status = response.statusCode || 500

          if (status >= 400) {
            let payload: unknown = {}

            try {
              payload = text ? JSON.parse(text) : {}
            } catch {
              // A malformed error body remains a typed HTTP failure, but its
              // raw content is never copied into the public error message.
            }

            reject(new BackendHttpError(status, safeBody(payload), text))

            return
          }

          if (!text) {
            resolve(null as T)

            return
          }

          const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
          const contentType = String(response.headers['content-type'] || '')

          if (looksHtml || contentType.includes('text/html')) {
            reject(new Error(`Expected JSON from Hermes backend (status ${status})`))

            return
          }

          try {
            resolve(JSON.parse(text) as T)
          } catch {
            reject(new Error(`Invalid JSON from Hermes backend (status ${status})`))
          }
        })
      }
    )

    outgoing.on('error', reject)
    outgoing.setTimeout(request.timeoutMs, () => {
      outgoing.destroy(
        new Error(`Timed out connecting to Hermes backend after ${request.timeoutMs}ms`)
      )
    })

    if (request.bodyBytes) {
      outgoing.write(request.bodyBytes)
    }

    outgoing.end()
  })
}
