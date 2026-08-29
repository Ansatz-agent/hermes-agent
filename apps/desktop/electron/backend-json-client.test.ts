import assert from 'node:assert/strict'
import http from 'node:http'

import { test, vi } from 'vitest'

import {
  BackendHttpError,
  requestJsonWithLocalCapability
} from './backend-json-client'
import type { BackendJsonTransportRequest } from './backend-json-client'
import type { LocalCapabilitySnapshot } from './local-capability-manager'

function snapshot(registrationId: string, bearer: string): LocalCapabilitySnapshot {
  return {
    key: 'primary',
    bearer,
    registrationId,
    scope: {
      connection_id: 'local',
      runtime_instance_id: '0123456789abcdef0123456789abcdef',
      epoch: 7
    },
    backendGeneration: 1,
    issuedAt: 100,
    rotateAt: 1_300,
    validUntil: 1_900
  }
}

function fakeManager(values: LocalCapabilitySnapshot[]) {
  let current = 0

  return {
    snapshot: vi.fn(() => values[current]),
    refresh: vi.fn(async () => {
      current = Math.min(current + 1, values.length - 1)

      return values[current]
    })
  }
}

test('retries one local pre-dispatch rejection with a newer confirmed descriptor', async () => {
  const oldToken = snapshot('old-registration', 'old-token')
  const newToken = snapshot('new-registration', 'new-token')
  const manager = fakeManager([oldToken, newToken])
  let handlerWrites = 0

  const transport = vi
    .fn()
    .mockRejectedValueOnce(
      new BackendHttpError(401, {
        code: 'local_capability_rejected',
        failure_phase: 'pre_dispatch',
        retryable: true
      })
    )
    .mockImplementationOnce(async request => {
      assert.equal(request.token, newToken.bearer)
      handlerWrites += 1

      return { ok: true }
    })

  const result = await requestJsonWithLocalCapability({
    manager,
    key: 'primary',
    url: 'http://127.0.0.1:43210/api/config',
    method: 'PUT',
    body: { model: 'test-model' },
    transport
  })

  assert.deepEqual(result, { ok: true })
  assert.equal(handlerWrites, 1)
  assert.equal(transport.mock.calls.length, 2)
  assert.equal(manager.refresh.mock.calls.length, 1)
  assert.deepEqual(manager.refresh.mock.calls[0], ['primary', 'recovery'])
})

test('performs the bounded recovery against a real HTTP backend', async () => {
  const oldToken = snapshot('old-registration', 'old-token')
  const newToken = snapshot('new-registration', 'new-token')
  const manager = fakeManager([oldToken, newToken])
  const observedTokens: string[] = []
  const observedBodies: Buffer[] = []
  let handlerWrites = 0

  const server = http.createServer((request, response) => {
    const chunks: Buffer[] = []

    request.on('data', chunk => chunks.push(Buffer.from(chunk)))
    request.on('end', () => {
      const token = String(request.headers['x-hermes-session-token'] || '')

      observedTokens.push(token)
      observedBodies.push(Buffer.concat(chunks))
      response.setHeader('Content-Type', 'application/json')

      if (token === oldToken.bearer) {
        response.statusCode = 401
        response.end(
          JSON.stringify({
            code: 'local_capability_rejected',
            failure_phase: 'pre_dispatch',
            retryable: true
          })
        )

        return
      }

      handlerWrites += 1
      response.end(JSON.stringify({ ok: true }))
    })
  })

  const port = await new Promise<number>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()

      assert.ok(address && typeof address !== 'string')
      resolve(address.port)
    })
  })

  try {
    const result = await requestJsonWithLocalCapability({
      manager,
      key: 'primary',
      url: `http://127.0.0.1:${port}/api/config`,
      method: 'PUT',
      body: { model: 'test-model' }
    })

    assert.deepEqual(result, { ok: true })
    assert.deepEqual(observedTokens, [oldToken.bearer, newToken.bearer])
    assert.equal(handlerWrites, 1)
    assert.equal(observedBodies.length, 2)
    assert.deepEqual(observedBodies[0], observedBodies[1])
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close(error => (error ? reject(error) : resolve()))
    })
  }
})

test('serializes a non-idempotent body once and reuses the exact bytes on recovery', async () => {
  const manager = fakeManager([
    snapshot('old-registration', 'old-token'),
    snapshot('new-registration', 'new-token')
  ])

  let modelReads = 0

  const body = {
    get model() {
      modelReads += 1

      return 'test-model'
    }
  }

  const observedBodies: Buffer[] = []

  const transport = vi.fn(async request => {
    observedBodies.push(request.bodyBytes)

    if (observedBodies.length === 1) {
      throw new BackendHttpError(401, {
        code: 'local_capability_rejected',
        failure_phase: 'pre_dispatch',
        retryable: true
      })
    }

    return { ok: true }
  })

  await requestJsonWithLocalCapability({
    manager,
    key: 'primary',
    url: 'http://127.0.0.1:43210/api/config',
    method: 'PUT',
    body,
    transport
  })

  assert.equal(modelReads, 1)
  assert.equal(observedBodies.length, 2)
  assert.equal(observedBodies[0], observedBodies[1])
  assert.equal(observedBodies[1].toString('utf8'), '{"model":"test-model"}')
})

test('preserves an explicitly requested long-running operation timeout', async () => {
  const manager = fakeManager([snapshot('one-registration', 'one-token')])
  const observedTimeouts: number[] = []

  const transport = vi.fn(async (request: BackendJsonTransportRequest) => {
    observedTimeouts.push(request.timeoutMs)

    return { ok: true }
  })

  const timeoutMs = 24 * 60 * 60_000

  await requestJsonWithLocalCapability({
    manager,
    key: 'primary',
    url: 'http://127.0.0.1:43210/api/cron/trigger',
    method: 'POST',
    timeoutMs,
    transport
  })

  assert.deepEqual(observedTimeouts, [timeoutMs])
})

test('never retries account, provider, malformed, post-dispatch, or network errors', async () => {
  const errors = [
    new BackendHttpError(401, { code: 'account_locked' }),
    new BackendHttpError(401, { code: 'provider_unauthorized' }),
    new BackendHttpError(500, {
      code: 'local_capability_rejected',
      failure_phase: 'post_dispatch',
      retryable: true
    }),
    new BackendHttpError(401, {
      code: 'local_capability_rejected',
      failure_phase: 'pre_dispatch',
      retryable: false
    }),
    new Error('network reset')
  ]

  for (const error of errors) {
    const manager = fakeManager([snapshot('one-registration', 'one-token')])
    const transport = vi.fn().mockRejectedValue(error)

    await assert.rejects(
      requestJsonWithLocalCapability({
        manager,
        key: 'primary',
        url: 'http://127.0.0.1:43210/api/config',
        method: 'PUT',
        body: { model: 'test-model' },
        transport
      }),
      candidate => candidate === error
    )

    assert.equal(transport.mock.calls.length, 1)
    assert.equal(manager.refresh.mock.calls.length, 0)
  }
})

test('does not replay when recovery returns the same registration', async () => {
  const current = snapshot('same-registration', 'same-token')
  const manager = fakeManager([current])

  const rejection = new BackendHttpError(401, {
    code: 'local_capability_rejected',
    failure_phase: 'pre_dispatch',
    retryable: true
  })

  const transport = vi.fn().mockRejectedValue(rejection)

  await assert.rejects(
    requestJsonWithLocalCapability({
      manager,
      key: 'primary',
      url: 'http://127.0.0.1:43210/api/config',
      method: 'PUT',
      body: { model: 'test-model' },
      transport
    }),
    candidate => candidate === rejection
  )

  assert.equal(transport.mock.calls.length, 1)
  assert.equal(manager.refresh.mock.calls.length, 1)
})

test('bounds foreground recovery by the request timeout and preserves the original rejection', async () => {
  vi.useFakeTimers()

  try {
    const current = snapshot('old-registration', 'old-token')

    const manager = {
      snapshot: vi.fn(() => current),
      refresh: vi.fn(() => new Promise<LocalCapabilitySnapshot>(() => {}))
    }

    const rejection = new BackendHttpError(401, {
      code: 'local_capability_rejected',
      failure_phase: 'pre_dispatch',
      retryable: true
    })

    const outcome = requestJsonWithLocalCapability({
      manager,
      key: 'primary',
      url: 'http://127.0.0.1:43210/api/config',
      method: 'PUT',
      body: { model: 'test-model' },
      timeoutMs: 500,
      transport: vi.fn().mockRejectedValue(rejection)
    }).then(
      value => ({ value }),
      error => ({ error })
    )

    await vi.advanceTimersByTimeAsync(499)
    let settled = false

    void outcome.then(() => {
      settled = true
    })
    await Promise.resolve()
    assert.equal(settled, false)

    await vi.advanceTimersByTimeAsync(1)
    assert.deepEqual(await outcome, { error: rejection })
    assert.equal(manager.refresh.mock.calls.length, 1)
  } finally {
    vi.useRealTimers()
  }
})

test('redacts and bounds backend error previews without changing provider classification', () => {
  const secret = 'provider-secret-sentinel'

  const error = new BackendHttpError(
    401,
    { code: 'provider_unauthorized', reason: 'invalid_api_key' },
    JSON.stringify({ bearer: secret, detail: 'x'.repeat(500) })
  )

  assert.equal(error.status, 401)
  assert.equal(error.code, 'provider_unauthorized')
  assert.equal(error.reason, 'invalid_api_key')
  assert.equal(error.failurePhase, null)
  assert.equal(error.retryable, false)
  assert.ok(error.bodyPreview.length <= 200)
  assert.equal(error.bodyPreview.includes(secret), false)
  assert.match(error.bodyPreview, /"bearer":"<redacted>"/)
  assert.equal(error.message.includes(secret), false)
})

test('redacts complete bearer and API credential values from JSON error previews', () => {
  const authorizationSecret = 'authorization-secret-sentinel'
  const apiKeySecret = 'api-key-secret-sentinel'
  const accessTokenSecret = 'access-token-secret-sentinel'

  const error = new BackendHttpError(
    401,
    { code: 'provider_unauthorized' },
    JSON.stringify({
      authorization: `Bearer ${authorizationSecret}`,
      api_key: apiKeySecret,
      access_token: accessTokenSecret
    })
  )

  for (const secret of [authorizationSecret, apiKeySecret, accessTokenSecret]) {
    assert.equal(error.bodyPreview.includes(secret), false)
    assert.equal(error.message.includes(secret), false)
  }

  assert.equal(error.bodyPreview.includes('Bearer'), false)
})

test('preserves a sanitized provider error body in the IPC-visible message', () => {
  const error = new BackendHttpError(
    400,
    {
      code: 'provider_unauthorized',
      detail: 'Invalid API key for provider openai'
    },
    JSON.stringify({
      detail: 'Invalid API key for provider openai',
      code: 'provider_unauthorized'
    })
  )

  assert.equal(error.name, 'Error')
  assert.match(error.message, /^400: /)
  assert.match(error.message, /Invalid API key for provider openai/)
  assert.match(error.message, /provider_unauthorized/)
})
