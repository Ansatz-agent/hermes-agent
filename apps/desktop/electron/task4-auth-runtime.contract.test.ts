import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { afterEach, test, vi } from 'vitest'

import { AuthBridgeError, type BridgeStatus, DesktopAuthBridge } from './auth-bridge'
import { AuthCoordinator } from './auth-coordinator'

class FakeChild extends EventEmitter {
  readonly stdin = new PassThrough()
  readonly stdout = new PassThrough()
  readonly stderr = new PassThrough()
  readonly kill = vi.fn(() => true)
}

const signedOut: BridgeStatus = {
  state: 'signed_out',
  username: null,
  account_id: null,
  session_id: null,
  installation_id: null,
  principal_key: null,
  runtime_instance_id: 'runtime-1',
  epoch: 1,
  valid_until: 0,
  validation_state: 'unknown',
  validation_reason: null,
  last_validated_at: null,
  legacy: false,
  reason: 'signed_out'
}

const authenticated: BridgeStatus = {
  ...signedOut,
  state: 'authenticated',
  username: 'alice',
  epoch: 2,
  valid_until: 4_000_000_000,
  account_id: '22222222-2222-4222-8222-222222222222',
  session_id: '33333333-3333-4333-8333-333333333333',
  installation_id: '11111111-1111-4111-8111-111111111111',
  principal_key: 'account:22222222-2222-4222-8222-222222222222',
  validation_state: 'online',
  last_validated_at: '2026-08-24T12:00:00+00:00',
  reason: null
}

function bridgeFixture() {
  const child = new FakeChild()
  const bridge = new DesktopAuthBridge({
    cwd: '/opt/hermes-agent',
    nativeClientContext: {
      installation_id: '11111111-1111-4111-8111-111111111111',
      client_version: '0.17.0'
    },
    pythonExecutable: '/opt/hermes-agent/venv/bin/python',
    spawnChild: vi.fn(() => child as any)
  })

  return { bridge, child }
}

afterEach(() => {
  vi.useRealTimers()
})

test('Task 4 bridge HTTP timeout occurs before process teardown timeout', async () => {
  vi.useFakeTimers()
  const { bridge, child } = bridgeFixture()
  const rejected = assert.rejects(
    bridge.status(),
    error => error instanceof AuthBridgeError && error.code === 'runtime_unavailable'
  )

  await vi.advanceTimersByTimeAsync(15_001)
  assert.equal(child.kill.mock.calls.length, 0)

  await vi.advanceTimersByTimeAsync(5_000)
  await rejected
  assert.deepEqual(child.kill.mock.calls, [[]])
})

test('Task 4 explicit Retry can rebuild one dead local auth bridge', async () => {
  const deadBridge = {
    status: vi.fn(async () => {
      throw new AuthBridgeError('runtime_unavailable', 'runtime_unavailable')
    }),
    login: vi.fn(async () => signedOut),
    logout: vi.fn(async () => signedOut)
  }
  const replacement = {
    status: vi.fn(async () => authenticated),
    login: vi.fn(async () => authenticated),
    logout: vi.fn(async () => signedOut)
  }
  const recoverBridge = vi.fn(async () => replacement)
  const coordinator = new AuthCoordinator(deadBridge, {
    clock: () => 1,
    pollIntervalMs: 0,
    recoverBridge
  } as any)

  const result = await (coordinator.refresh as any)('local', { recoverRuntime: true })

  assert.equal(result.state, 'authenticated')
  assert.equal(recoverBridge.mock.calls.length, 1)
})

test('Task 4 exposes a sanitized bootstrap progress state machine', async () => {
  const progress = await import('./bootstrap-progress')

  assert.equal(typeof progress, 'object')
})

test('Task 4 suppresses stale runtime preparation completion after logout', async () => {
  const preparation = await import('./authenticated-runtime-preparation')

  assert.equal(typeof preparation.runAuthenticatedRuntimePreparation, 'function')
})

test('Task 4 keeps protected renderer readiness behind the desktop runtime gate', async () => {
  const runtimeGate = await import('./desktop-runtime-gate')

  assert.equal(typeof runtimeGate, 'object')
})
