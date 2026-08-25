import { randomBytes, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import https from 'node:https'

const ORIGIN = 'https://c2sml.cn' as const
const LOGIN_PATH = '/auth/login/'
const SESSION_PATH = '/auth/api/session/'
const NATIVE_SESSION_PATH = '/auth/api/client-session/'
const NATIVE_CURRENT_SESSION_PATH = '/auth/api/client-session/current/'
const NATIVE_TRACE_TOKEN_PATH = '/auth/api/client-session/trace-token/'
const LOGOUT_PATH = '/auth/logout/'
const CSRF_COOKIE = '__Host-ansatz_csrftoken'
const SESSION_COOKIE = '__Host-ansatz_sessionid'
const MAX_FORM_BYTES = 64 * 1024

export type AuthServiceMode = 'online' | 'timeout' | '429' | '500' | 'malformed'
export type ExplicitRevocationReason = 'account_disabled' | 'account_revoked' | 'session_revoked'

type AuthEventName =
  | 'login_page'
  | 'login_rejected'
  | 'login_accepted'
  | 'session_valid'
  | 'native_session_issued'
  | 'native_session_valid'
  | 'trace_token_issued'
  | 'native_session_deleted'
  | 'revoked'
  | 'revocation_response'
  | 'held_timeout'
  | 'logout'

export interface FixedAuthIdentity {
  accountId: string
  sessionId: string
  installationId: string
}

export interface FixedAuthContractServer {
  origin: string
  listenPort: number
  username: string
  password: string
  invalidPassword: string
  sensitiveValues(): string[]
  events(): ReadonlyArray<{ name: AuthEventName; at: number }>
  diagnostics(): readonly string[]
  heldRequestCount(): number
  setMode(mode: AuthServiceMode): void
  revokeCurrent(reason: ExplicitRevocationReason): void
  currentIdentity(): FixedAuthIdentity
  close(): Promise<void>
}

function token(bytes = 24): string {
  return randomBytes(bytes).toString('base64url')
}

function cookies(request: IncomingMessage): Map<string, string> {
  const values = new Map<string, string>()
  for (const part of (request.headers.cookie ?? '').split(';')) {
    const separator = part.indexOf('=')
    if (separator <= 0) {
      continue
    }
    values.set(part.slice(0, separator).trim(), part.slice(separator + 1).trim())
  }
  return values
}

function send(
  response: ServerResponse,
  status: number,
  body: string,
  headers: Record<string, string | string[]> = {}
): void {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
    ...headers
  })
  response.end(body)
}

function sendJson(response: ServerResponse, status: number, body: object): void {
  send(response, status, JSON.stringify(body), { 'Content-Type': 'application/json' })
}

async function readForm(request: IncomingMessage): Promise<URLSearchParams> {
  if (
    (request.headers['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase() !==
    'application/x-www-form-urlencoded'
  ) {
    throw new Error('invalid form content type')
  }

  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of request) {
    const buffer = Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_FORM_BYTES) {
      throw new Error('form body is too large')
    }
    chunks.push(buffer)
  }

  return new URLSearchParams(Buffer.concat(chunks).toString('utf8'))
}

function sameOriginReferer(request: IncomingMessage): boolean {
  const value = request.headers.referer
  if (!value) {
    return false
  }
  try {
    const referer = new URL(value)
    return (
      referer.protocol === 'https:' &&
      referer.hostname === new URL(ORIGIN).hostname &&
      referer.host === request.headers.host &&
      referer.pathname.startsWith('/auth/')
    )
  } catch {
    return false
  }
}

function validCsrf(request: IncomingMessage, form: URLSearchParams, expectedCsrf: string | null): boolean {
  if (!expectedCsrf || !sameOriginReferer(request)) {
    return false
  }
  const header = request.headers['x-csrftoken']
  const headerValue = Array.isArray(header) ? '' : header

  return (
    headerValue === expectedCsrf &&
    form.get('csrfmiddlewaretoken') === expectedCsrf &&
    cookies(request).get(CSRF_COOKIE) === expectedCsrf
  )
}

export async function startFixedAuthContractServer(options: {
  certPath: string
  keyPath: string
  listenPort?: number
}): Promise<FixedAuthContractServer> {
  const username = `hermes-e2e-${token(8)}`
  const password = token()
  const invalidPassword = token()
  const sensitive = new Set([password, invalidPassword])
  const recordedEvents: Array<{ name: AuthEventName; at: number }> = []
  const recordedDiagnostics: string[] = []
  const accountId = randomUUID()
  let sessionId: string = randomUUID()
  let installationId: string = randomUUID()
  let nativeSessionToken: string | null = null
  let revoked: { reason: ExplicitRevocationReason; revokedAt: string } | null = null
  let mode: AuthServiceMode = 'online'
  let closing = false
  let currentCsrf: string | null = null
  let currentSession: string | null = null
  const held = new Set<{ response: ServerResponse; resume: () => void }>()

  const record = (name: AuthEventName): void => {
    recordedEvents.push({ name, at: Date.now() })
  }

  const diagnostic = (message: string): void => {
    recordedDiagnostics.push(message)
  }

  const releaseHeld = (): void => {
    for (const entry of held) {
      entry.resume()
    }
    held.clear()
  }

  const applyMode = async (requestPath: string, response: ServerResponse): Promise<boolean> => {
    if (mode === 'online') {
      return false
    }
    if (mode === 'timeout') {
      record('held_timeout')
      diagnostic(`held ${requestPath}`)
      await new Promise<void>(resolve => held.add({ response, resume: resolve }))
      if (closing || response.destroyed) {
        return true
      }
      return applyMode(requestPath, response)
    }
    if (mode === '429') {
      diagnostic(`429 ${requestPath}`)
      sendJson(response, 429, { detail: 'rate_limited' })
      return true
    }
    if (mode === '500') {
      diagnostic(`500 ${requestPath}`)
      sendJson(response, 500, { detail: 'server_unavailable' })
      return true
    }

    diagnostic(`malformed ${requestPath}`)
    send(response, 200, '{malformed', {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store'
    })
    return true
  }

  const validNativeRequest = (request: IncomingMessage): boolean => {
    const authorization = request.headers.authorization
    const requestInstallation = request.headers['x-ansatz-installation-id']

    return (
      nativeSessionToken !== null &&
      authorization === `Bearer ${nativeSessionToken}` &&
      requestInstallation === installationId &&
      request.headers.cookie === undefined &&
      request.headers['x-csrftoken'] === undefined
    )
  }

  const sendInvalidNativeCredential = (response: ServerResponse): void => {
    sendJson(response, 401, {
      state: 'unavailable',
      code: 'invalid_session_credential',
      retryable: true
    })
  }

  const sendRevocation = (response: ServerResponse, requestPath: string): void => {
    if (!revoked) {
      throw new Error('revocation response requested without a revocation')
    }
    record('revocation_response')
    diagnostic(`revocation response ${revoked.reason} ${requestPath}`)
    sendJson(response, 403, {
      state: 'revoked',
      code: revoked.reason,
      account_id: accountId,
      session_id: sessionId,
      revoked_at: revoked.revokedAt,
      retryable: false
    })
  }

  const server = https.createServer(
    {
      cert: fs.readFileSync(options.certPath),
      key: fs.readFileSync(options.keyPath)
    },
    async (request, response) => {
      const method = request.method ?? ''
      const requestPath = new URL(request.url ?? '/', ORIGIN).pathname

      if (requestPath.startsWith('/auth/') && (await applyMode(requestPath, response))) {
        return
      }

      if (method === 'GET' && requestPath === LOGIN_PATH) {
        currentCsrf = token()
        sensitive.add(currentCsrf)
        record('login_page')
        const body = `<!doctype html><html><body><form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="${currentCsrf}"></form></body></html>`
        send(response, 200, body, {
          'Content-Type': 'text/html; charset=utf-8',
          'Set-Cookie': `${CSRF_COOKIE}=${currentCsrf}; Path=/; Secure; SameSite=Lax`
        })
        return
      }

      if (method === 'POST' && requestPath === LOGIN_PATH) {
        let form: URLSearchParams
        try {
          form = await readForm(request)
        } catch {
          send(response, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' })
          return
        }
        if (!validCsrf(request, form, currentCsrf)) {
          send(response, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' })
          return
        }
        if (form.get('username') !== username || form.get('password') !== password) {
          record('login_rejected')
          send(response, 200, '<!doctype html><html><body>Invalid credentials</body></html>', {
            'Content-Type': 'text/html; charset=utf-8'
          })
          return
        }

        currentSession = token()
        sensitive.add(currentSession)
        record('login_accepted')
        send(response, 302, '', {
          Location: '/traces/',
          'Set-Cookie': `${SESSION_COOKIE}=${currentSession}; Path=/; Secure; HttpOnly; SameSite=Lax`
        })
        return
      }

      if (method === 'GET' && requestPath === SESSION_PATH) {
        if (!currentSession || cookies(request).get(SESSION_COOKIE) !== currentSession) {
          sendJson(response, 401, { authenticated: false })
          return
        }

        const serverTime = new Date()
        record('session_valid')
        sendJson(response, 200, {
          authenticated: true,
          sub: '7',
          username,
          role: 'user',
          server_time: serverTime.toISOString(),
          session_expires_at: new Date(serverTime.getTime() + 60 * 60 * 1000).toISOString(),
          trace_dashboard_url: '/traces/'
        })
        return
      }

      if (method === 'POST' && requestPath === NATIVE_SESSION_PATH) {
        const requestCookies = cookies(request)
        if (
          !currentSession ||
          requestCookies.get(SESSION_COOKIE) !== currentSession ||
          !currentCsrf ||
          requestCookies.get(CSRF_COOKIE) !== currentCsrf ||
          request.headers['x-csrftoken'] !== currentCsrf ||
          !sameOriginReferer(request) ||
          (request.headers['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase() !== 'application/json'
        ) {
          sendJson(response, 401, { detail: 'invalid_cookie_session' })
          return
        }

        let body: unknown
        try {
          const chunks: Buffer[] = []
          let size = 0
          for await (const chunk of request) {
            const buffer = Buffer.from(chunk)
            size += buffer.length
            if (size > MAX_FORM_BYTES) {
              throw new Error('native session body is too large')
            }
            chunks.push(buffer)
          }
          body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
        } catch {
          sendJson(response, 400, { detail: 'invalid_request' })
          return
        }

        if (
          !body ||
          typeof body !== 'object' ||
          Array.isArray(body) ||
          Object.keys(body).sort().join(',') !== 'client_version,installation_id' ||
          typeof (body as { client_version?: unknown }).client_version !== 'string' ||
          typeof (body as { installation_id?: unknown }).installation_id !== 'string' ||
          !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
            (body as { installation_id: string }).installation_id
          )
        ) {
          sendJson(response, 400, { detail: 'invalid_request' })
          return
        }

        sessionId = randomUUID()
        installationId = (body as { installation_id: string }).installation_id
        nativeSessionToken = token(32)
        sensitive.add(nativeSessionToken)
        revoked = null
        record('native_session_issued')
        sendJson(response, 201, {
          account_id: accountId,
          session_id: sessionId,
          session_token: nativeSessionToken,
          installation_id: installationId,
          username,
          issued_at: new Date().toISOString()
        })
        return
      }

      if (method === 'GET' && requestPath === NATIVE_SESSION_PATH) {
        if (!validNativeRequest(request)) {
          sendInvalidNativeCredential(response)
          return
        }
        if (revoked) {
          sendRevocation(response, requestPath)
          return
        }
        record('native_session_valid')
        sendJson(response, 200, {
          state: 'active',
          account_id: accountId,
          session_id: sessionId,
          installation_id: installationId,
          username,
          server_time: new Date().toISOString()
        })
        return
      }

      if (method === 'POST' && requestPath === NATIVE_TRACE_TOKEN_PATH) {
        if (!validNativeRequest(request)) {
          sendInvalidNativeCredential(response)
          return
        }
        if (revoked) {
          sendRevocation(response, requestPath)
          return
        }
        const accessToken = `trace-${token(32)}`
        sensitive.add(accessToken)
        record('trace_token_issued')
        sendJson(response, 200, {
          access_token: accessToken,
          expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
          expires_in: 900,
          installation_id: installationId
        })
        return
      }

      if (method === 'DELETE' && requestPath === NATIVE_CURRENT_SESSION_PATH) {
        if (!validNativeRequest(request)) {
          sendInvalidNativeCredential(response)
          return
        }
        nativeSessionToken = null
        revoked = null
        record('native_session_deleted')
        send(response, 204, '')
        return
      }

      if (method === 'POST' && requestPath === LOGOUT_PATH) {
        let form: URLSearchParams
        try {
          form = await readForm(request)
        } catch {
          send(response, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' })
          return
        }
        const requestCookies = cookies(request)
        if (
          !validCsrf(request, form, currentCsrf) ||
          !currentSession ||
          requestCookies.get(SESSION_COOKIE) !== currentSession
        ) {
          send(response, 403, 'Forbidden', { 'Content-Type': 'text/plain; charset=utf-8' })
          return
        }

        currentSession = null
        record('logout')
        send(response, 302, '', {
          Location: '/auth/login/',
          'Set-Cookie': `${SESSION_COOKIE}=; Path=/; Secure; HttpOnly; Max-Age=0; SameSite=Lax`
        })
        return
      }

      send(response, 404, 'Not Found', { 'Content-Type': 'text/plain; charset=utf-8' })
    }
  )

  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error): void => reject(error)
    server.once('error', onError)
    server.listen(options.listenPort ?? 443, '127.0.0.1', () => {
      server.off('error', onError)
      resolve()
    })
  })
  const address = server.address()
  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Fixed auth contract server did not bind a TCP port')
  }

  const origin = address.port === 443 ? ORIGIN : `${ORIGIN}:${address.port}`

  return {
    origin,
    listenPort: address.port,
    username,
    password,
    invalidPassword,
    sensitiveValues: () => [...sensitive],
    events: () => recordedEvents.map(event => ({ ...event })),
    diagnostics: () => [...recordedDiagnostics],
    heldRequestCount: () => held.size,
    setMode: nextMode => {
      mode = nextMode
      diagnostic(`mode ${nextMode}`)
      releaseHeld()
    },
    revokeCurrent: reason => {
      if (!nativeSessionToken) {
        throw new Error('Cannot revoke before a native Session is issued')
      }
      revoked = { reason, revokedAt: new Date().toISOString() }
      record('revoked')
      diagnostic(`revoked ${reason}`)
      releaseHeld()
    },
    currentIdentity: () => ({ accountId, sessionId, installationId }),
    close: () =>
      new Promise<void>((resolve, reject) => {
        closing = true
        for (const entry of held) {
          entry.response.destroy()
          entry.resume()
        }
        held.clear()
        server.close(error => (error ? reject(error) : resolve()))
        server.closeAllConnections()
      })
  }
}
