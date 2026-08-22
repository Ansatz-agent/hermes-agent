import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import https from 'node:https'

const ORIGIN = 'https://c2sml.cn' as const
const LOGIN_PATH = '/agent/accounts/login/'
const SESSION_PATH = '/agent/api/session/'
const LOGOUT_PATH = '/agent/accounts/logout/'
const CSRF_COOKIE = 'agent_history_csrftoken'
const SESSION_COOKIE = 'agent_history_sessionid'
const MAX_FORM_BYTES = 64 * 1024

type AuthEventName = 'login_page' | 'login_rejected' | 'login_accepted' | 'session_valid' | 'logout'

export interface FixedAuthContractServer {
  origin: 'https://c2sml.cn'
  listenPort: number
  username: string
  password: string
  invalidPassword: string
  sensitiveValues(): string[]
  events(): ReadonlyArray<{ name: AuthEventName; at: number }>
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
  headers: Record<string, string | string[]> = {},
): void {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
    ...headers,
  })
  response.end(body)
}

function sendJson(response: ServerResponse, status: number, body: object): void {
  send(response, status, JSON.stringify(body), { 'Content-Type': 'application/json' })
}

async function readForm(request: IncomingMessage): Promise<URLSearchParams> {
  if ((request.headers['content-type'] ?? '').split(';', 1)[0].trim().toLowerCase() !== 'application/x-www-form-urlencoded') {
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
    return referer.origin === ORIGIN && referer.pathname.startsWith('/agent/')
  } catch {
    return false
  }
}

function validCsrf(
  request: IncomingMessage,
  form: URLSearchParams,
  expectedCsrf: string | null,
): boolean {
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
  let currentCsrf: string | null = null
  let currentSession: string | null = null

  const record = (name: AuthEventName): void => {
    recordedEvents.push({ name, at: Date.now() })
  }

  const server = https.createServer(
    {
      cert: fs.readFileSync(options.certPath),
      key: fs.readFileSync(options.keyPath),
    },
    async (request, response) => {
      const method = request.method ?? ''
      const requestPath = new URL(request.url ?? '/', ORIGIN).pathname

      if (method === 'GET' && requestPath === LOGIN_PATH) {
        currentCsrf = token()
        sensitive.add(currentCsrf)
        record('login_page')
        const body = `<!doctype html><html><body><form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="${currentCsrf}"></form></body></html>`
        send(response, 200, body, {
          'Content-Type': 'text/html; charset=utf-8',
          'Set-Cookie': `${CSRF_COOKIE}=${currentCsrf}; Path=/agent/; Secure; SameSite=Lax`,
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
            'Content-Type': 'text/html; charset=utf-8',
          })
          return
        }

        currentSession = token()
        sensitive.add(currentSession)
        record('login_accepted')
        send(response, 302, '', {
          Location: '/agent/',
          'Set-Cookie': `${SESSION_COOKIE}=${currentSession}; Path=/agent/; Secure; HttpOnly; SameSite=Lax`,
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
          username,
          server_time: serverTime.toISOString(),
          session_expires_at: new Date(serverTime.getTime() + 60 * 60 * 1000).toISOString(),
        })
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
          Location: '/agent/',
          'Set-Cookie': `${SESSION_COOKIE}=; Path=/agent/; Secure; HttpOnly; Max-Age=0; SameSite=Lax`,
        })
        return
      }

      send(response, 404, 'Not Found', { 'Content-Type': 'text/plain; charset=utf-8' })
    },
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

  return {
    origin: ORIGIN,
    listenPort: address.port,
    username,
    password,
    invalidPassword,
    sensitiveValues: () => [...sensitive],
    events: () => recordedEvents.map(event => ({ ...event })),
    close: () => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve())
    }),
  }
}
