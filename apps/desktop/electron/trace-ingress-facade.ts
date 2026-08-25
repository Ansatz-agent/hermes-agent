import { randomBytes, timingSafeEqual } from 'node:crypto'
import http, { type IncomingMessage, type ServerResponse } from 'node:http'

export type TraceIngressEndpoint = { endpoint: string; localBearer: string }

type Delegate = TraceIngressEndpoint

const UNAVAILABLE_MESSAGE = Buffer.from('trace durability temporarily unavailable', 'utf8')

const UNAVAILABLE_STATUS = Buffer.concat([
  Buffer.from([0x08, 0x0e, 0x12]),
  encodeVarint(UNAVAILABLE_MESSAGE.length),
  UNAVAILABLE_MESSAGE
])

export class TraceIngressFacade {
  private delegate: Delegate | null = null
  private generation = 0
  private localBearer = ''
  private server: http.Server | null = null

  async start(): Promise<TraceIngressEndpoint> {
    if (this.server !== null) {
      throw new Error('trace_ingress_facade_already_started')
    }

    this.localBearer = randomBytes(32).toString('base64url')
    const server = http.createServer((request, response) => void this.handle(request, response))
    this.server = server

    try {
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
    } catch (error) {
      this.server = null
      this.localBearer = ''
      throw error
    }

    const address = server.address()

    if (!address || typeof address === 'string') {
      await this.stop()
      throw new Error('trace_ingress_facade_unavailable')
    }

    return { endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: this.localBearer }
  }

  // Invalidates the stable bearer when the bound account/session detaches so
  // a surviving old producer cannot inject into a later account. The endpoint
  // is unchanged; freshly attached backends receive the rotated bearer.
  rotateBearer(): TraceIngressEndpoint | null {
    const server = this.server

    if (server === null) {
      return null
    }

    const address = server.address()

    if (!address || typeof address === 'string') {
      return null
    }

    this.localBearer = randomBytes(32).toString('base64url')

    return { endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: this.localBearer }
  }

  install(delegate: Delegate): void {
    validateDelegate(delegate)
    this.generation += 1
    this.delegate = { ...delegate }
  }

  detach(): void {
    this.generation += 1
    this.delegate = null
  }

  async stop(): Promise<void> {
    this.detach()
    const server = this.server
    this.server = null
    this.localBearer = ''

    if (server !== null) {
      const closed = new Promise<void>(resolve => server.close(() => resolve()))
      server.closeAllConnections()
      await closed
    }
  }

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    let upstream: http.ClientRequest | null = null

    // A producer can disconnect at any time. Its request/response streams
    // emit 'error' (ECONNRESET/EPIPE); leaving those unhandled would crash
    // the main process, and leaving the upstream request open would leak a
    // socket and any buffered body.
    const abortUpstream = () => {
      upstream?.destroy()
    }

    request.once('error', abortUpstream)
    response.once('error', abortUpstream)
    response.once('close', () => {
      if (!response.writableFinished) {
        abortUpstream()
      }
    })

    if (!matchesBearer(request.headers.authorization, this.localBearer)) {
      request.resume()
      response.writeHead(401, { 'cache-control': 'no-store', 'content-length': '0' })
      response.end()

      return
    }

    const delegate = this.delegate
    const generation = this.generation

    if (delegate === null) {
      request.resume()
      respondUnavailable(response)

      return
    }

    try {
      const target = new URL(delegate.endpoint)
      const headers = { ...request.headers, authorization: `Bearer ${delegate.localBearer}` }

      upstream = http.request(target, { headers, method: request.method }, delegateResponse => {
        delegateResponse.once('error', () => {
          if (!response.headersSent) {
            respondUnavailable(response)
          } else {
            response.destroy()
          }
        })

        if (generation !== this.generation || delegate !== this.delegate) {
          delegateResponse.resume()

          if (!response.headersSent) {
            respondUnavailable(response)
          } else {
            response.end()
          }

          return
        }

        response.writeHead(delegateResponse.statusCode ?? 503, delegateResponse.headers)
        delegateResponse.pipe(response)
      })

      upstream.once('error', () => {
        if (!response.headersSent) {
          respondUnavailable(response)
        } else {
          response.end()
        }
      })
      request.pipe(upstream)
    } catch {
      request.resume()
      respondUnavailable(response)
    }
  }
}

export function respondTraceUnavailable(response: ServerResponse): void {
  respondUnavailable(response)
}

function respondUnavailable(response: ServerResponse): void {
  if (response.destroyed || response.writableEnded) {
    return
  }

  response.writeHead(503, {
    'cache-control': 'no-store',
    'content-length': String(UNAVAILABLE_STATUS.length),
    'content-type': 'application/x-protobuf',
    'retry-after': '1'
  })
  response.end(UNAVAILABLE_STATUS)
}

function validateDelegate(delegate: Delegate): void {
  const endpoint = new URL(delegate.endpoint)

  if (
    endpoint.protocol !== 'http:' ||
    endpoint.hostname !== '127.0.0.1' ||
    endpoint.pathname !== '/v1/traces' ||
    endpoint.search !== '' ||
    endpoint.hash !== '' ||
    !endpoint.port ||
    !/^[0-9A-Za-z_-]{43}$/.test(delegate.localBearer) ||
    Buffer.from(delegate.localBearer, 'base64url').byteLength !== 32
  ) {
    throw new TypeError('invalid_trace_ingress_delegate')
  }
}

function matchesBearer(authorization: string | undefined, bearer: string): boolean {
  if (!authorization || !bearer) {
    return false
  }

  const expected = Buffer.from(`Bearer ${bearer}`)
  const actual = Buffer.from(authorization)

  return actual.length === expected.length && timingSafeEqual(actual, expected)
}

function encodeVarint(value: number): Buffer {
  const bytes: number[] = []
  let remaining = value

  do {
    const current = remaining & 0x7f
    remaining >>>= 7
    bytes.push(remaining === 0 ? current : current | 0x80)
  } while (remaining !== 0)

  return Buffer.from(bytes)
}
