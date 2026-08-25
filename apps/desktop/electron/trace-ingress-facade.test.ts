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
