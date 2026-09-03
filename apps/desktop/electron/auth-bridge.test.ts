import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { afterEach, test, vi } from 'vitest'

import { AuthBridgeError, DesktopAuthBridge, sameConnectionScope } from './auth-bridge'

const authenticatedStatus = {
  state: 'authenticated' as const,
  username: 'alice',
  account_id: '22222222-2222-4222-8222-222222222222',
  session_id: '33333333-3333-4333-8333-333333333333',
  installation_id: '11111111-1111-4111-8111-111111111111',
  principal_key: 'account:22222222-2222-4222-8222-222222222222',
  runtime_instance_id: 'runtime-1',
  epoch: 3,
  valid_until: 42,
  cloud_state: 'active' as const,
  validation_state: 'online' as const,
  validation_reason: null,
  last_validated_at: '2026-08-24T12:00:00+00:00',
  legacy: false,
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

const traceCredentialNow = Date.parse('2099-08-23T14:00:00Z')

test('connection scope equality requires the exact connection, runtime, and epoch tuple', () => {
  const value = { connection_id: 'local', epoch: 7, runtime_instance_id: 'runtime-a' }

  assert.equal(sameConnectionScope(value, { ...value }), true)
  assert.equal(sameConnectionScope(value, { ...value, connection_id: 'remote' }), false)
  assert.equal(sameConnectionScope(value, { ...value, runtime_instance_id: 'runtime-b' }), false)
  assert.equal(sameConnectionScope(value, { ...value, epoch: 8 }), false)
  assert.equal(sameConnectionScope(value, null), false)
})

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
      AGENT_HISTORY_SESSIONID: 'agent_history_sessionid=do-not-leak',
      HERMES_AUTH_KEYRING_SERVICE: 'cn.c2sml.ansatz.voice-trace-client.remote-auth',
      HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'cn.c2sml.hermes.remote-auth',
      HERMES_AUTH_RUNTIME_NAMESPACE: 'ansatz-voice-trace-client-auth-v1',
      HERMES_AUTH_UNREVIEWED_VALUE: 'must-not-cross-owner-boundary',
      HERMES_HOME: '/home/alice/.hermes',
      PATH: '/usr/bin',
      PROVIDER_API_KEY: 'provider-secret',
      SSH_CONNECTION: '127.0.0.1 40000 127.0.0.1 22'
    },
    onDiagnostic: message => diagnostics.push(message),
    nativeClientContext: {
      installation_id: '11111111-1111-4111-8111-111111111111',
      client_version: '0.17.0'
    },
    pythonExecutable: '/opt/hermes-agent/venv/bin/python',
    spawnChild,
    clock: () => traceCredentialNow,
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

test('status carries native context and accepts degraded health without secrets', async () => {
  const child = new FakeChild()

  const bridge = new DesktopAuthBridge({
    cwd: '/repo',
    pythonExecutable: '/python',
    nativeClientContext: {
      installation_id: '11111111-1111-4111-8111-111111111111',
      client_version: '0.17.0'
    },
    spawnChild: () => child as any
  })

  const pending = bridge.status()
  const request = await readRequest(child)

  assert.deepEqual(request.params, {
    installation_id: '11111111-1111-4111-8111-111111111111',
    client_version: '0.17.0'
  })
  respond(child, {
    version: 2,
    id: request.id,
    result: {
      ...authenticatedStatus,
      valid_until: 253_402_300_799,
      cloud_state: 'unreachable',
      validation_state: 'degraded',
      validation_reason: 'server_unavailable'
    }
  })

  const status = await pending
  assert.equal(status.cloud_state, 'unreachable')
  assert.equal(status.validation_state, 'degraded')
  assert.equal(status.valid_until, 253_402_300_799)
  assert.equal(JSON.stringify(status).includes('session_token'), false)
  bridge.close()
})

test('status accepts authenticated local continuity while cloud reauth is required', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.status()
  const request = await readRequest(child)

  respond(child, {
    version: 2,
    id: request.id,
    result: {
      ...authenticatedStatus,
      valid_until: 0,
      cloud_state: 'reauth_required',
      validation_state: 'degraded',
      validation_reason: 'session_expired',
      reason: 'session_expired'
    }
  })

  const status = await pending
  assert.equal(status.state, 'authenticated')
  assert.equal(status.cloud_state, 'reauth_required')
  bridge.close()
})

test.each(['account_disabled', 'account_revoked', 'session_revoked'])(
  'status accepts the matching-identity %s terminal snapshot',
  async reason => {
    const { bridge, child } = bridgeFixture()
    const pending = bridge.status()
    const request = await readRequest(child)

    const terminal = {
      ...authenticatedStatus,
      state: 'locked',
      username: null,
      valid_until: 0,
      cloud_state: null,
      validation_state: 'degraded',
      validation_reason: reason,
      reason
    }

    respond(child, { version: 2, id: request.id, result: terminal })

    assert.deepEqual(await pending, terminal)
    bridge.close()
  }
)

test('login injects native context while leaving renderer credentials out of status frames', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.login('alice', 'password-sentinel')
  const request = await readRequest(child)

  assert.deepEqual(request.params, {
    username: 'alice',
    password: 'password-sentinel',
    installation_id: '11111111-1111-4111-8111-111111111111',
    client_version: '0.17.0'
  })
  respond(child, { version: 2, id: request.id, result: authenticatedStatus })

  const status = await pending
  assert.equal(JSON.stringify(status).includes('password-sentinel'), false)
  bridge.close()
})

test('status frames with secret or extra fields fail closed', async () => {
  const { bridge, child, diagnostics } = bridgeFixture()
  const pending = bridge.status()
  const request = await readRequest(child)

  respond(child, { version: 2, id: request.id, result: { ...authenticatedStatus, session_token: 'secret-sentinel' } })

  await assert.rejects(
    pending,
    error =>
      error instanceof AuthBridgeError &&
      error.code === 'runtime_unavailable' &&
      !error.message.includes('secret-sentinel')
  )
  assert.equal(diagnostics.join('\n').includes('secret-sentinel'), false)
})

test.each([
  ['missing', ({ cloud_state: _cloudState, ...status }) => status],
  ['unknown', status => ({ ...status, cloud_state: 'maybe' })]
] as const)('status frames with %s cloud_state fail closed', async (_name, mutate) => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.status()
  const request = await readRequest(child)

  respond(child, { version: 2, id: request.id, result: mutate(authenticatedStatus) })

  await assert.rejects(pending, error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable')
})

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
          HERMES_AUTH_KEYRING_SERVICE: 'cn.c2sml.ansatz.voice-trace-client.remote-auth',
          HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'cn.c2sml.hermes.remote-auth',
          HERMES_AUTH_RUNTIME_NAMESPACE: 'ansatz-voice-trace-client-auth-v1',
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
  assert.deepEqual(request, {
    version: 2,
    id: '1',
    method: 'status',
    params: { installation_id: '11111111-1111-4111-8111-111111111111', client_version: '0.17.0' }
  })
  assert.equal(JSON.stringify(spawnChild.mock.calls).includes('serve'), false)

  respond(child, { version: 2, id: '1', result: authenticatedStatus })
  assert.deepEqual(await pending, authenticatedStatus)
  bridge.close()
})

test('uses monotonically increasing bounded request ids', async () => {
  const { bridge, child } = bridgeFixture()

  const first = bridge.status()
  const firstRequest = await readRequest(child)
  respond(child, { version: 2, id: firstRequest.id, result: authenticatedStatus })
  await first

  const second = bridge.logout()
  const secondRequest = await readRequest(child)
  respond(child, {
    version: 2,
    id: secondRequest.id,
    result: {
      ...authenticatedStatus,
      state: 'signed_out',
      username: null,
      epoch: 4,
      valid_until: 0,
      cloud_state: null,
      validation_state: 'unknown',
      last_validated_at: null,
      reason: 'signed_out'
    }
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
    version: 2,
    id: '1',
    method: 'trace_token',
    params: traceRequest
  })

  respond(child, { version: 2, id: '1', result: traceCredential })
  assert.deepEqual(await pending, traceCredential)
  bridge.close()
})

test('requests one exact Desktop ingress lease without a cloud credential', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.traceIngress({ entrypoint: 'desktop', consumer_id: 'desktop-local' })
  const request = await readRequest(child)

  const lease = {
    endpoint: 'http://127.0.0.1:49152/v1/traces',
    authorization: `Bearer ${'a'.repeat(43)}`,
    installation_id: traceRequest.installation_id,
    entrypoint: 'desktop' as const,
    plugins_toml: '/opt/Ansatz/config/ansatz-voice-trace/plugins.toml'
  }

  assert.deepEqual(request, {
    version: 2,
    id: '1',
    method: 'trace_ingress_open',
    params: { entrypoint: 'desktop', consumer_id: 'desktop-local' }
  })
  respond(child, { version: 2, id: '1', result: lease })

  assert.deepEqual(await pending, lease)
  assert.equal(JSON.stringify(lease).includes('access_token'), false)
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

    respond(child, { version: 2, id: request.id, result })
    await rejected
    assert.equal(diagnostics.join('\n').includes(traceCredential.access_token), false)
  }
})

test('Trace credentials reject a bearer that expires at the injected bridge clock', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.traceToken(traceRequest)
  const request = await readRequest(child)

  respond(child, {
    version: 1,
    id: request.id,
    result: { ...traceCredential, expires_at: '2099-08-23T14:00:00+00:00' }
  })
  await assert.rejects(pending, error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable')
  bridge.close()
})

test('Trace credentials reject impossible, far-future, and lifetime-mismatched expiry values', async () => {
  for (const expiresAt of ['2099-02-30T14:15:00+00:00', '9999-08-23T14:15:00+00:00', '2099-08-23T14:15:30.001+00:00']) {
    const { bridge, child } = bridgeFixture()
    const pending = bridge.traceToken(traceRequest)
    const request = await readRequest(child)

    respond(child, { version: 2, id: request.id, result: { ...traceCredential, expires_at: expiresAt } })
    await assert.rejects(pending, error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable')
  }
})

test('Trace credential expiry accepts the documented positive clock-skew boundary', async () => {
  const { bridge, child } = bridgeFixture()
  const pending = bridge.traceToken(traceRequest)
  const request = await readRequest(child)
  const boundary = { ...traceCredential, expires_at: '2099-08-23T14:15:30+00:00' }

  respond(child, { version: 2, id: request.id, result: boundary })
  assert.deepEqual(await pending, boundary)
  bridge.close()
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
    `${JSON.stringify({ version: 1, id: '1', result: authenticatedStatus })}\n`,
    `${JSON.stringify({ version: 2, id: 'unexpected', result: authenticatedStatus })}\n`
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
    assert.equal(diagnostics.join('\n').includes('agent_history_sessionid'), false)
  }
})

test('oversized response, child exit, and stderr all fail closed without leaking child output', async () => {
  for (const breakChild of [
    (child: FakeChild) => child.stdout.write(Buffer.alloc(64 * 1024 + 1, 120)),
    (child: FakeChild) => child.emit('exit', 1, null),
    (child: FakeChild) => {
      child.stderr.write('agent_history_sessionid=stderr-secret')
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
  assert.equal(diagnostics.join('\n').includes('agent_history_sessionid'), false)
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
  respond(child, { version: 2, id: request.id, result: authenticatedStatus })
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
    version: 2,
    id: request.id,
    result: {
      ...authenticatedStatus,
      state: 'signed_out',
      username: null,
      valid_until: 0,
      cloud_state: null,
      validation_state: 'unknown',
      last_validated_at: null,
      reason: 'signed_out'
    }
  })
  assert.equal((await pending).state, 'signed_out')
  bridge.close()
})
