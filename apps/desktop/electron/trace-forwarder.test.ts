import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'

import { test } from 'vitest'

import {
  DEFAULT_TRACE_UPSTREAM_URL,
  isExpectedTraceShutdownError,
  RefreshingTraceCredentialProvider,
  type TraceCredentialSource,
  TraceForwarder
} from './trace-forwarder'

const installationId = '11111111-1111-4111-8111-111111111111'
const protobuf = Buffer.from([0x0a, 0x03, 0x01, 0x02, 0x03])
const traceCredentialNow = Date.parse('2099-08-23T14:00:00+00:00')

test('product Trace uploads use the public same-origin Gateway API by default', () => {
  assert.equal(DEFAULT_TRACE_UPSTREAM_URL, 'https://c2sml.cn/trace-ingest/v1/traces')
})

async function post(
  endpoint: string,
  localBearer: string,
  overrides: {
    body?: Buffer
    headers?: Record<string, string>
    includeCorrelationHeaders?: boolean
  } = {}
) {
  const target = new URL(endpoint)
  const body = overrides.body ?? protobuf

  const correlationHeaders =
    overrides.includeCorrelationHeaders === false
      ? {}
      : {
          'x-hermes-session-id': 'session-1',
          'x-trace-entrypoint': 'desktop',
          'x-trace-run-id': 'run-1',
          'x-telemetry-schema-version': '1'
        }

  const headers = {
    authorization: `Bearer ${localBearer}`,
    'content-type': 'application/x-protobuf',
    ...correlationHeaders,
    ...overrides.headers
  }

  return new Promise<{ body: Buffer; status: number }>((resolve, reject) => {
    const request = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        path: target.pathname,
        method: 'POST',
        headers
      },
      response => {
        const chunks: Buffer[] = []

        response.on('error', reject)
        response.on('data', chunk => chunks.push(Buffer.from(chunk)))
        response.on('end', () => resolve({ body: Buffer.concat(chunks), status: response.statusCode ?? 0 }))
      }
    )

    request.on('error', reject)
    request.on('socket', socket => socket.on('error', reject))
    request.end(body)
  })
}

function varint(value: number): Buffer {
  const bytes: number[] = []
  let remaining = value

  do {
    const byte = remaining & 0x7f

    remaining = Math.floor(remaining / 128)
    bytes.push(remaining > 0 ? byte | 0x80 : byte)
  } while (remaining > 0)

  return Buffer.from(bytes)
}

function lengthDelimited(field: number, value: Buffer | string): Buffer {
  const body = typeof value === 'string' ? Buffer.from(value) : value

  return Buffer.concat([varint((field << 3) | 2), varint(body.length), body])
}

function keyValue(key: string, value: string): Buffer {
  const anyValue = lengthDelimited(1, value)

  return Buffer.concat([lengthDelimited(1, key), lengthDelimited(2, anyValue)])
}

function relayOtlpPayload(sessionId: string, traceIdHex: string): Buffer {
  const traceId = Buffer.from(traceIdHex, 'hex')
  const spanId = Buffer.from('1122334455667788', 'hex')
  const session = keyValue('nemo_relay.scope.metadata.session_id', sessionId)

  const span = Buffer.concat([
    lengthDelimited(1, traceId),
    lengthDelimited(2, spanId),
    lengthDelimited(5, 'hermes.turn'),
    lengthDelimited(9, session)
  ])

  const scopeSpans = lengthDelimited(2, span)
  const resourceSpans = lengthDelimited(2, scopeSpans)

  return lengthDelimited(1, resourceSpans)
}

async function waitFor(predicate: () => boolean) {
  const deadline = Date.now() + 2_000

  while (!predicate()) {
    if (Date.now() >= deadline) {
      assert.fail('timed out waiting for forwarder')
    }

    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

function credentialSource(): TraceCredentialSource & { calls: boolean[] } {
  return {
    calls: [],
    async load(forceRefresh) {
      this.calls.push(forceRefresh)

      return {
        access_token: forceRefresh
          ? 'public-trace-token-refreshed-1234567890'
          : 'public-trace-token-initial-1234567890',
        expires_at: '2099-08-23T14:15:00+00:00',
        expires_in: 900,
        installation_id: installationId
      }
    }
  }
}

test('credential provider caches until 60 seconds before expiry and supports forced rotation', async () => {
  let now = Date.parse('2099-08-23T14:00:00+00:00')
  const source = credentialSource()
  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => now })

  const first = await provider.current()
  assert.equal(await provider.current(), first)
  now = Date.parse(first.expires_at) - 60_000
  await provider.current()
  await provider.current({ forceRefresh: true })

  assert.deepEqual(source.calls, [false, false, true])
})

test('loopback forwarder accepts exact protobuf and adds only the public bearer upstream', async () => {
  const source = credentialSource()
  const calls: Array<{ body: Buffer; headers: Headers }> = []

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(source, { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      calls.push({ body: Buffer.from(init?.body as Buffer), headers: new Headers(init?.headers) })

      return new Response(Buffer.alloc(0), {
        status: 200,
        headers: { 'content-type': 'application/x-protobuf' }
      })
    },
    installationId
  })

  const started = await forwarder.start(7)

  try {
    const response = await post(started.endpoint, started.localBearer)
    assert.equal(response.status, 200)
    await waitFor(() => calls.length === 1)
    assert.deepEqual(calls[0].body, protobuf)
    assert.equal(calls[0].headers.get('authorization'), 'Bearer public-trace-token-initial-1234567890')
    assert.equal(calls[0].headers.get('x-hermes-session-id'), 'session-1')
    assert.equal(calls[0].headers.get('x-trace-run-id'), 'run-1')
    assert.equal(calls[0].headers.has('x-local-authorization'), false)
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
  }
})

test('real Relay OTLP without custom correlation headers is canonicalized for the Gateway', async () => {
  const calls: Headers[] = []
  const traceId = '00112233445566778899aabbccddeeff'

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async (_input, init) => {
      calls.push(new Headers(init?.headers))

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId
  })

  const started = await forwarder.start(7)

  try {
    const response = await post(started.endpoint, started.localBearer, {
      body: relayOtlpPayload('desktop-session-real', traceId),
      includeCorrelationHeaders: false
    })

    assert.equal(response.status, 200)
    await waitFor(() => calls.length === 1)
    assert.equal(calls[0].get('x-hermes-session-id'), 'desktop-session-real')
    assert.equal(calls[0].get('x-trace-run-id'), traceId)
    assert.equal(calls[0].get('x-trace-entrypoint'), 'desktop')
    assert.equal(calls[0].get('x-telemetry-schema-version'), '1')
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
  }
})

test('one upstream 401 forces one credential refresh and resends identical bytes once', async () => {
  const source = credentialSource()
  const bodies: Buffer[] = []
  const authorizations: string[] = []

  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => traceCredentialNow })
  const invalidate = provider.invalidate.bind(provider)

  let invalidations = 0

  provider.invalidate = () => {
    invalidations += 1
    invalidate()
  }

  const forwarder = new TraceForwarder({
    credentialProvider: provider,
    fetchImpl: async (_input, init) => {
      bodies.push(Buffer.from(init?.body as Buffer))
      authorizations.push(new Headers(init?.headers).get('authorization') ?? '')

      return new Response(Buffer.alloc(0), { status: bodies.length === 1 ? 401 : 200 })
    },
    installationId
  })

  const started = await forwarder.start(7)

  try {
    assert.equal((await post(started.endpoint, started.localBearer)).status, 200)
    await waitFor(() => bodies.length === 2)
    assert.deepEqual(bodies[0], bodies[1])
    assert.deepEqual(authorizations, [
      'Bearer public-trace-token-initial-1234567890',
      'Bearer public-trace-token-refreshed-1234567890'
    ])
    assert.deepEqual(source.calls, [false, true])
    assert.equal(invalidations, 2)
  } finally {
    await forwarder.stop({ flushMs: 3_000 })
  }
})

test('HTTP boundary rejects remote peers, bad local auth, media drift, encoding, oversize, and stale epoch', async () => {
  let remoteAddress = '127.0.0.1'
  let upstreamCalls = 0

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource(), { clock: () => traceCredentialNow }),
    fetchImpl: async () => {
      upstreamCalls += 1

      return new Response(Buffer.alloc(0), { status: 200 })
    },
    installationId,
    remoteAddressForRequest: () => remoteAddress
  })

  const first = await forwarder.start(7)

  remoteAddress = '192.0.2.10'
  assert.equal((await post(first.endpoint, first.localBearer)).status, 403)
  remoteAddress = '127.0.0.1'
  assert.equal((await post(first.endpoint, 'wrong-local-bearer')).status, 401)
  assert.equal(
    (await post(first.endpoint, first.localBearer, { headers: { 'content-type': 'application/json' } })).status,
    415
  )
  assert.equal((await post(first.endpoint, first.localBearer, { headers: { 'content-encoding': 'gzip' } })).status, 415)
  assert.equal((await post(first.endpoint, first.localBearer, { body: Buffer.alloc(8 * 1024 * 1024 + 1) })).status, 413)
  await forwarder.stop({ flushMs: 0 })

  const second = await forwarder.start(8)

  try {
    assert.equal((await post(second.endpoint, first.localBearer)).status, 401)
    assert.equal(upstreamCalls, 0)
  } finally {
    await forwarder.stop({ flushMs: 0 })
  }
})

test('stop remains bounded when a local OTLP client holds an incomplete request open', async () => {
  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource()),
    installationId
  })
  const started = await forwarder.start(7)
  const target = new URL(started.endpoint)
  const request = http.request({
    hostname: target.hostname,
    port: target.port,
    path: target.pathname,
    method: 'POST',
    headers: {
      authorization: `Bearer ${started.localBearer}`,
      'content-length': '5',
      'content-type': 'application/x-protobuf'
    }
  })
  request.on('error', () => {})
  const connected = new Promise<void>(resolve => {
    request.on('socket', socket => {
      if (socket.readyState === 'open') {
        resolve()
      } else {
        socket.once('connect', resolve)
      }
    })
  })
  request.flushHeaders()
  await connected

  const stopping = forwarder.stop({ flushMs: 0 })

  try {
    const stoppedWithinBoundary = await Promise.race([
      stopping.then(() => true),
      new Promise<false>(resolve => setTimeout(() => resolve(false), 250))
    ])
    assert.equal(stoppedWithinBoundary, true)
  } finally {
    request.destroy()
    await stopping
  }
})

test('stop consumes only expected socket errors with oversized and held requests', async () => {
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('reset'), { code: 'ECONNRESET' }), true), true)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('pipe'), { code: 'EPIPE' }), true), true)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('reset'), { code: 'ECONNRESET' }), false), false)
  assert.equal(isExpectedTraceShutdownError(Object.assign(new Error('other'), { code: 'ENOSPC' }), true), false)

  const forwarder = new TraceForwarder({
    credentialProvider: new RefreshingTraceCredentialProvider(credentialSource()),
    installationId,
    maxBodyBytes: 4
  })
  const started = await forwarder.start(7)
  const target = new URL(started.endpoint)
  const requests = [Buffer.alloc(5), Buffer.from([0x0a])].map((body, index) => {
    const request = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname,
      method: 'POST',
      headers: {
        authorization: `Bearer ${started.localBearer}`,
        ...(index === 0 ? {} : { 'content-length': '5' }),
        'content-type': 'application/x-protobuf'
      }
    })
    request.on('error', error => {
      assert.ok(['ECONNRESET', 'EPIPE'].includes((error as NodeJS.ErrnoException).code ?? ''))
    })
    request.on('socket', socket => {
      socket.on('error', error => {
        assert.ok(['ECONNRESET', 'EPIPE'].includes((error as NodeJS.ErrnoException).code ?? ''))
      })
    })
    request.write(body)
    request.flushHeaders()

    return request
  })

  const stopping = forwarder.stop({ flushMs: 0 })

  try {
    assert.equal(
      await Promise.race([
        stopping.then(() => true),
        new Promise<false>(resolve => setTimeout(() => resolve(false), 250))
      ]),
      true
    )
  } finally {
    for (const request of requests) {
      request.destroy()
    }
    await stopping
  }
})

test('desktop lifecycle starts Trace before local spawn and flushes it before backend teardown', () => {
  const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  const prepareStart = source.indexOf('prepareLocalBackend: async () => {')
  const prepareEnd = source.indexOf('resolveRemote:', prepareStart)
  const prepare = source.slice(prepareStart, prepareEnd)

  assert.ok(prepareStart >= 0)
  assert.ok(
    prepare.indexOf('await ensureDesktopTraceForwarder(connectionScope)') <
      prepare.indexOf('return resolveHermesBackend(backendArgs)')
  )

  const cleanupStart = source.indexOf("async function cleanupDesktopCapabilities(connectionId = 'local')")
  const cleanupEnd = source.indexOf('function enableDesktopCapabilityShell()', cleanupStart)
  const cleanup = source.slice(cleanupStart, cleanupEnd)

  assert.ok(cleanupStart >= 0)
  assert.ok(
    cleanup.indexOf('await stopDesktopTraceForwarder(3_000)') <
      cleanup.indexOf('teardownPrimaryBackendAndWait({ soft: true })')
  )
  assert.match(source, /trace: traceContextForBackendRoot\(root\)/)
  assert.match(source, /trace: traceContextForBackendRoot\(ACTIVE_HERMES_ROOT\)/)
  assert.match(source, /pluginsToml: path\.join\(root, 'config', 'ansatz-voice-trace', 'plugins\.toml'\)/)
})
