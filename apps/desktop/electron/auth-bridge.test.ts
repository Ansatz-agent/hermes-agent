import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { afterEach, test, vi } from 'vitest'

import { AuthBridgeError, DesktopAuthBridge } from './auth-bridge'

const authenticatedStatus = {
  state: 'authenticated' as const,
  username: 'alice',
  runtime_instance_id: 'runtime-1',
  epoch: 3,
  valid_until: 42,
  session_expires_at: '2026-08-18T13:00:00+00:00',
  reason: null
}

const traceRequest = {
  installation_id: '11111111-1111-4111-8111-111111111111',
  client_version: '0.17.0',
  telemetry_schema_version: '1'
}

const traceCredential = {
  access_token: 'trace-token-sentinel-1234567890',
  expires_at: '2099-08-23T14:15:00+00:00',
  expires_in: 900,
  installation_id: traceRequest.installation_id
}

class FakeChild extends EventEmitter {
  readonly stdin = new PassThrough()
  readonly stdout = new PassThrough()
  readonly stderr = new PassThrough()
  readonly kill = vi.fn(() => true)
}

function bridgeFixture(overrides: Record<string, unknown> = {}) {
  const child = new FakeChild()
  const diagnostics: string[] = []
  const spawnChild = vi.fn(() => child as any)

  const bridge = new DesktopAuthBridge({
    cwd: '/opt/hermes-agent',
    env: {
      ANSATZ_SESSIONID: '__Host-ansatz_sessionid=do-not-leak',
      HERMES_HOME: '/home/alice/.hermes',
      PATH: '/usr/bin',
      PROVIDER_API_KEY: 'provider-secret',
      SSH_CONNECTION: '127.0.0.1 40000 127.0.0.1 22'
    },
    onDiagnostic: message => diagnostics.push(message),
    pythonExecutable: '/opt/hermes-agent/venv/bin/python',
    spawnChild,
    ...overrides
  })

  return { bridge, child, diagnostics, spawnChild }
}

function readRequest(child: FakeChild): Promise<Record<string, unknown>> {
  return new Promise(resolve => {
    child.stdin.once('data', chunk => resolve(JSON.parse(String(chunk))))
  })
}

function respond(child: FakeChild, payload: unknown) {
  child.stdout.write(`${JSON.stringify(payload)}\n`)
}

afterEach(() => {
  vi.useRealTimers()
})

test('starts only the closed auth module with bounded stdio and a secret-free environment', async () => {
  const { bridge, child, spawnChild } = bridgeFixture()

  assert.deepEqual(spawnChild.mock.calls, [
    [
      '/opt/hermes-agent/venv/bin/python',
      ['-m', 'hermes_cli.client_auth.bridge'],
      {
        cwd: '/opt/hermes-agent',
        env: {
          HERMES_HOME: '/home/alice/.hermes',
          PATH: '/usr/bin',
          SSH_CONNECTION: '127.0.0.1 40000 127.0.0.1 22'
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true
      }
    ]
  ])

  const pending = bridge.status()
  const request = await readRequest(child)
  assert.deepEqual(request, { version: 1, id: '1', method: 'status', params: {} })
  assert.equal(JSON.stringify(spawnChild.mock.calls).includes('serve'), false)

  respond(child, { version: 1, id: '1', result: authenticatedStatus })
  assert.deepEqual(await pending, authenticatedStatus)
  bridge.close()
})

test('uses monotonically increasing bounded request ids', async () => {
  const { bridge, child } = bridgeFixture()

  const first = bridge.status()
  const firstRequest = await readRequest(child)
  respond(child, { version: 1, id: firstRequest.id, result: authenticatedStatus })
  await first

  const second = bridge.logout()
  const secondRequest = await readRequest(child)
  respond(child, {
    version: 1,
    id: secondRequest.id,
    result: { ...authenticatedStatus, state: 'signed_out', username: null, epoch: 4, reason: 'signed_out' }
  })
  await second

  assert.deepEqual([firstRequest.id, secondRequest.id], ['1', '2'])
  assert.ok(String(secondRequest.id).length <= 64)
  bridge.close()
})

test('requests one short-lived Trace credential with exact installation identity', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.traceToken(traceRequest)
  const request = await readRequest(child)

  assert.deepEqual(request, {
    version: 1,
    id: '1',
    method: 'trace_token',
    params: traceRequest
  })

  respond(child, { version: 1, id: '1', result: traceCredential })
  assert.deepEqual(await pending, traceCredential)
  bridge.close()
})

test('Trace credentials fail closed on malformed, stale, or extra response fields', async () => {
  for (const result of [
    { ...traceCredential, access_token: '' },
    { ...traceCredential, expires_in: 0 },
    { ...traceCredential, expires_at: '2000-08-23T14:15:00+00:00' },
    { ...traceCredential, expires_at: 'not-a-date' },
    { ...traceCredential, installation_id: '22222222-2222-4222-8222-222222222222' },
    { ...traceCredential, unexpected: true }
  ]) {
    const { bridge, child, diagnostics } = bridgeFixture()
    const pending = bridge.traceToken(traceRequest)
    const request = await readRequest(child)

    const rejected = assert.rejects(
      pending,
      error =>
        error instanceof AuthBridgeError &&
        error.code === 'runtime_unavailable' &&
        !error.message.includes(traceCredential.access_token)
    )

    respond(child, { version: 1, id: request.id, result })
    await rejected
    assert.equal(diagnostics.join('\n').includes(traceCredential.access_token), false)
  }
})

test('rejects invalid Trace requests before writing to the auth child', async () => {
  const { bridge, child } = bridgeFixture()
  const writes: unknown[] = []

  child.stdin.on('data', chunk => writes.push(chunk))

  await assert.rejects(
    bridge.traceToken({ ...traceRequest, installation_id: 'not-a-uuid' }),
    error => error instanceof AuthBridgeError && error.code === 'invalid_request'
  )
  await assert.rejects(
    bridge.traceToken({ ...traceRequest, telemetry_schema_version: '1', extra: 'field' } as any),
    error => error instanceof AuthBridgeError && error.code === 'invalid_request'
  )
  assert.deepEqual(writes, [])
  bridge.close()
})

test('rejects unknown methods and fields before writing stdin', async () => {
  const { bridge, child } = bridgeFixture()
  const writes: unknown[] = []
  child.stdin.on('data', chunk => writes.push(chunk))

  await assert.rejects(
    bridge.invoke({ method: 'signup', params: {} } as any),
    error => error instanceof AuthBridgeError && error.code === 'invalid_request'
  )
  await assert.rejects(
    bridge.invoke({ method: 'status', params: {}, command: 'hermes serve' } as any),
    error => error instanceof AuthBridgeError && error.code === 'invalid_request'
  )
  assert.deepEqual(writes, [])
  bridge.close()
})

test('malformed json and schema drift fail every request with a redacted runtime error', async () => {
  for (const response of [
    'not-json\n',
    `${JSON.stringify({ version: 2, id: '1', result: authenticatedStatus })}\n`,
    `${JSON.stringify({ version: 1, id: 'unexpected', result: authenticatedStatus })}\n`
  ]) {
    const { bridge, child, diagnostics } = bridgeFixture()
    const first = bridge.login('alice', 'password-sentinel')
    const second = bridge.status()

    const rejections = [first, second].map(pending =>
      assert.rejects(
        pending,
        error =>
          error instanceof AuthBridgeError &&
          error.code === 'runtime_unavailable' &&
          !error.message.includes('password-sentinel')
      )
    )

    child.stdout.write(response)

    await Promise.all(rejections)
    assert.equal(diagnostics.join('\n').includes('password-sentinel'), false)
    assert.equal(diagnostics.join('\n').includes('__Host-ansatz_sessionid'), false)
  }
})

test('oversized response, child exit, and stderr all fail closed without leaking child output', async () => {
  for (const breakChild of [
    (child: FakeChild) => child.stdout.write(Buffer.alloc(64 * 1024 + 1, 120)),
    (child: FakeChild) => child.emit('exit', 1, null),
    (child: FakeChild) => {
      child.stderr.write('__Host-ansatz_sessionid=stderr-secret')
      child.emit('close', 1, null)
    }
  ]) {
    const { bridge, child, diagnostics } = bridgeFixture()
    const pending = bridge.login('alice', 'password-sentinel')

    const rejected = assert.rejects(
      pending,
      error =>
        error instanceof AuthBridgeError &&
        error.code === 'runtime_unavailable' &&
        !error.message.includes('stderr-secret')
    )

    breakChild(child)

    await rejected
    assert.equal(diagnostics.join('\n').includes('stderr-secret'), false)
    assert.equal(diagnostics.join('\n').includes('password-sentinel'), false)
  }
})

test('a request timeout rejects all pending calls and terminates the bridge', async () => {
  vi.useFakeTimers()
  const { bridge, child, diagnostics } = bridgeFixture({ timeoutMs: 15_000 })
  const first = bridge.login('alice', 'password-sentinel')
  const second = bridge.status()

  const rejections = [first, second].map(pending =>
    assert.rejects(pending, error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable')
  )

  await vi.advanceTimersByTimeAsync(15_001)

  await Promise.all(rejections)
  assert.deepEqual(child.kill.mock.calls, [[]])
  assert.equal(diagnostics.join('\n').includes('password-sentinel'), false)
  assert.equal(diagnostics.join('\n').includes('__Host-ansatz_sessionid'), false)
})

test('the default bridge deadline leaves room for the Python HTTP timeout to respond', async () => {
  vi.useFakeTimers()
  const { bridge, child } = bridgeFixture()
  const pending = bridge.status()

  const rejected = assert.rejects(
    pending,
    error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable'
  )

  await vi.advanceTimersByTimeAsync(15_001)
  assert.equal(child.kill.mock.calls.length, 0)

  await vi.advanceTimersByTimeAsync(5_000)
  await rejected
  assert.deepEqual(child.kill.mock.calls, [[]])
})

test('login has a longer bounded deadline than credential-free status', async () => {
  vi.useFakeTimers()
  const { bridge, child } = bridgeFixture()
  const pending = bridge.login('alice', 'password-sentinel')

  await vi.advanceTimersByTimeAsync(20_001)
  assert.equal(child.kill.mock.calls.length, 0)

  const request = JSON.parse(String(child.stdin.read()))
  respond(child, { version: 1, id: request.id, result: authenticatedStatus })
  assert.deepEqual(await pending, authenticatedStatus)
  bridge.close()
})

test('logout recovery has room to clear the local session after owner restart', async () => {
  vi.useFakeTimers()
  const { bridge, child } = bridgeFixture()
  const pending = bridge.logout()
  void pending.catch(() => {})

  await vi.advanceTimersByTimeAsync(36_001)
  assert.equal(child.kill.mock.calls.length, 0)

  const request = JSON.parse(String(child.stdin.read()))
  respond(child, {
    version: 1,
    id: request.id,
    result: { ...authenticatedStatus, state: 'signed_out', username: null, reason: 'signed_out' }
  })
  assert.equal((await pending).state, 'signed_out')
  bridge.close()
})
