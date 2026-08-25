import assert from 'node:assert/strict'
import http from 'node:http'

import { test } from 'vitest'

import { TraceIngressFacade } from './trace-ingress-facade'

const payload = Buffer.from([0x0a, 0x03, 0x01, 0x02, 0x03])

test('facade returns retryable OTLP UNAVAILABLE until a durable delegate is installed', async () => {
  const facade = new TraceIngressFacade()
  const ingress = await facade.start()
  const received: Buffer[] = []

  const unavailable = await post(ingress.endpoint, ingress.localBearer)
  assert.equal(unavailable.status, 503)
  assert.equal(unavailable.headers.get('content-type'), 'application/x-protobuf')
  assert.equal(unavailable.headers.get('retry-after'), '1')
  assert.deepEqual(Buffer.from(await unavailable.arrayBuffer()).subarray(0, 2), Buffer.from([0x08, 0x0e]))

  const delegate = http.createServer((request, response) => {
    const chunks: Buffer[] = []
    request.on('data', chunk => chunks.push(Buffer.from(chunk)))
    request.on('end', () => {
      received.push(Buffer.concat(chunks))
      response.writeHead(200, { 'content-length': '0', 'content-type': 'application/x-protobuf' })
      response.end()
    })
  })

  await new Promise<void>((resolve, reject) => {
    delegate.once('error', reject)
    delegate.listen(0, '127.0.0.1', resolve)
  })
  const address = delegate.address()
  assert.ok(address && typeof address !== 'string')
  facade.install({ endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: 'a'.repeat(43) })

  try {
    const recovered = await post(ingress.endpoint, ingress.localBearer)
    assert.equal(recovered.status, 200)
    assert.deepEqual(received, [payload])
  } finally {
    facade.detach()
    await facade.stop()
    await new Promise<void>(resolve => delegate.close(() => resolve()))
  }
})

test('an aborted producer never crashes the facade and tears down the upstream request', async () => {
  const facade = new TraceIngressFacade()
  const ingress = await facade.start()
  const uncaught: unknown[] = []

  const onUncaught = (error: unknown) => {
    uncaught.push(error)
  }

  process.on('uncaughtException', onUncaught)

  let releaseUpstream!: () => void

  const upstreamClosed = new Promise<void>(resolve => {
    releaseUpstream = resolve
  })

  const delegate = http.createServer((request, response) => {
    request.on('error', () => {})
    response.on('error', () => {})
    // Hold the proxied request open until the facade tears it down.
    request.socket.once('close', () => releaseUpstream())
  })

  await new Promise<void>((resolve, reject) => {
    delegate.once('error', reject)
    delegate.listen(0, '127.0.0.1', resolve)
  })
  const address = delegate.address()
  assert.ok(address && typeof address !== 'string')
  facade.install({ endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: 'a'.repeat(43) })

  try {
    const target = new URL(ingress.endpoint)

    const producer = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname,
      method: 'POST',
      headers: {
        authorization: `Bearer ${ingress.localBearer}`,
        'content-length': '64',
        'content-type': 'application/x-protobuf'
      }
    })

    producer.on('error', () => {})

    const connected = new Promise<void>(resolve => {
      producer.on('socket', socket => {
        if (socket.readyState === 'open') {
          resolve()
        } else {
          socket.once('connect', resolve)
        }
      })
    })

    producer.write(Buffer.from([0x0a]))
    producer.flushHeaders()
    await connected
    await new Promise(resolve => setTimeout(resolve, 50))
    producer.destroy()

    const tornDown = await Promise.race([
      upstreamClosed.then(() => true),
      new Promise<false>(resolve => setTimeout(() => resolve(false), 1_000))
    ])

    await new Promise(resolve => setTimeout(resolve, 50))

    assert.deepEqual(uncaught, [])
    assert.equal(tornDown, true, 'facade must abort the upstream request when the producer disconnects')
  } finally {
    process.off('uncaughtException', onUncaught)
    await facade.stop()
    await new Promise<void>(resolve => delegate.close(() => resolve()))
  }
})

test('an upstream failure mid-response ends the producer response without crashing', async () => {
  const facade = new TraceIngressFacade()
  const ingress = await facade.start()
  const uncaught: unknown[] = []

  const onUncaught = (error: unknown) => {
    uncaught.push(error)
  }

  process.on('uncaughtException', onUncaught)

  const delegate = http.createServer((request, response) => {
    request.on('error', () => {})
    response.on('error', () => {})
    request.on('end', () => {
      response.writeHead(200, { 'content-type': 'application/x-protobuf' })
      response.write(Buffer.from([0x0a]))
      setTimeout(() => response.destroy(), 20)
    })
    request.resume()
  })

  await new Promise<void>((resolve, reject) => {
    delegate.once('error', reject)
    delegate.listen(0, '127.0.0.1', resolve)
  })
  const address = delegate.address()
  assert.ok(address && typeof address !== 'string')
  facade.install({ endpoint: `http://127.0.0.1:${address.port}/v1/traces`, localBearer: 'a'.repeat(43) })

  try {
    const outcome = await post(ingress.endpoint, ingress.localBearer)
      .then(response => response.arrayBuffer())
      .then(
        () => 'completed',
        () => 'aborted'
      )

    await new Promise(resolve => setTimeout(resolve, 100))

    assert.deepEqual(uncaught, [])
    assert.ok(outcome === 'completed' || outcome === 'aborted')

    // The facade must remain usable for the next producer.
    const followup = await post(ingress.endpoint, ingress.localBearer)
    assert.ok(followup.status === 200 || followup.status === 503)
  } finally {
    process.off('uncaughtException', onUncaught)
    await facade.stop()
    await new Promise<void>(resolve => delegate.close(() => resolve()))
  }
})

async function post(endpoint: string, bearer: string): Promise<Response> {
  return fetch(endpoint, {
    method: 'POST',
    body: payload,
    headers: {
      authorization: `Bearer ${bearer}`,
      'content-type': 'application/x-protobuf',
      'x-hermes-session-id': 'session-facade',
      'x-telemetry-schema-version': '1',
      'x-trace-entrypoint': 'desktop',
      'x-trace-run-id': 'run-facade'
    }
  })
}
