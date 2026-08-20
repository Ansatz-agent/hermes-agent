import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { AuthBridgeError, type BridgeStatus } from './auth-bridge'
import { AuthCoordinator, CoordinatorAuthRequiredError } from './auth-coordinator'

const signedOut: BridgeStatus = {
  state: 'signed_out',
  username: null,
  runtime_instance_id: 'runtime-1',
  epoch: 1,
  valid_until: 0,
  session_expires_at: null,
  reason: 'signed_out'
}

const authenticated: BridgeStatus = {
  ...signedOut,
  state: 'authenticated',
  username: 'alice',
  epoch: 2,
  valid_until: 90,
  session_expires_at: '2026-08-18T13:00:00+00:00',
  reason: null
}

function fixture(initial = signedOut, clock = () => 42) {
  let nextStatus = initial

  const bridge = {
    status: vi.fn(async () => nextStatus),
    login: vi.fn(async () => authenticated),
    logout: vi.fn(async () => signedOut)
  }

  const cleanup = vi.fn(async () => {})
  const coordinator = new AuthCoordinator(bridge, { cleanup, clock, pollIntervalMs: 0 })

  return {
    bridge,
    cleanup,
    coordinator,
    setStatus(status: BridgeStatus) {
      nextStatus = status
    }
  }
}

function statusFor(runtime: string, epoch: number, state: BridgeStatus['state'] = 'authenticated'): BridgeStatus {
  return {
    ...authenticated,
    state,
    username: state === 'authenticated' ? 'alice' : null,
    runtime_instance_id: runtime,
    epoch,
    valid_until: state === 'authenticated' ? 90 : 0,
    reason: state === 'authenticated' ? null : 'signed_out'
  }
}

function fixedBridge(status: BridgeStatus) {
  return {
    status: vi.fn(async () => status),
    login: vi.fn(async () => status),
    logout: vi.fn(async () => ({ ...status, state: 'signed_out' as const, username: null, valid_until: 0 }))
  }
}

test('stores a full scope only after the bridge reports authenticated', async () => {
  const { coordinator } = fixture()
  await coordinator.start()

  assert.equal(coordinator.status().state, 'signed_out')
  await assert.rejects(
    coordinator.require('local', 'local'),
    error => error instanceof CoordinatorAuthRequiredError && error.code === 'AUTH_REQUIRED'
  )

  await coordinator.login('alice', 'password-sentinel')

  assert.deepEqual(coordinator.scope('local'), {
    connection_id: 'local',
    runtime_instance_id: 'runtime-1',
    epoch: 2
  })
  await assert.doesNotReject(coordinator.require('local', 'local'))
})

test('logout invalidates and emits locked before cleanup or bridge logout', async () => {
  const { bridge, cleanup, coordinator } = fixture(authenticated)
  const order: string[] = []
  coordinator.subscribe(status => order.push(`event:${status.state}`))
  cleanup.mockImplementation(async () => {
    assert.equal(coordinator.scope('local'), null)
    order.push('cleanup')
  })
  bridge.logout.mockImplementation(async () => {
    order.push('bridge:logout')

    return signedOut
  })

  await coordinator.start()
  order.length = 0
  await coordinator.logout()

  assert.deepEqual(order, ['event:locked', 'cleanup', 'bridge:logout', 'event:signed_out'])
  assert.equal(coordinator.scope('local'), null)
})

test('serializes refresh behind logout cleanup so polling cannot restore a revoked scope', async () => {
  let releaseCleanup: (() => void) | null = null
  let cleanupStarted: (() => void) | null = null
  let ownerStatus = authenticated

  const cleanupEntered = new Promise<void>(resolve => {
    cleanupStarted = resolve
  })

  const cleanupBlocked = new Promise<void>(resolve => {
    releaseCleanup = resolve
  })

  const bridge = {
    status: vi.fn(async () => ownerStatus),
    login: vi.fn(async () => authenticated),
    logout: vi.fn(async () => {
      ownerStatus = signedOut

      return signedOut
    })
  }

  const cleanup = vi.fn(async () => {
    cleanupStarted?.()
    await cleanupBlocked
  })

  const coordinator = new AuthCoordinator(bridge, { cleanup, clock: () => 42, pollIntervalMs: 0 })
  await coordinator.start()

  const logout = coordinator.logout()
  await cleanupEntered
  const refresh = coordinator.refresh()

  assert.equal(bridge.status.mock.calls.length, 1)
  assert.equal(coordinator.scope('local'), null)
  releaseCleanup?.()
  await logout
  await refresh

  assert.equal(bridge.logout.mock.calls.length, 1)
  assert.equal(bridge.status.mock.calls.length, 2)
  assert.equal(coordinator.status().state, 'signed_out')
  assert.equal(coordinator.scope('local'), null)
})

test('an owner epoch change locks the old scope before protected work', async () => {
  const { cleanup, coordinator, setStatus } = fixture(authenticated)
  await coordinator.start()
  const oldScope = coordinator.scope('local')

  setStatus({ ...authenticated, state: 'signed_out', username: null, epoch: 3, reason: 'session_expired' })
  await coordinator.refresh()

  assert.equal(coordinator.scope('local'), null)
  assert.equal(coordinator.status().state, 'signed_out')
  assert.equal(cleanup.mock.calls.length, 1)
  await assert.rejects(coordinator.requireScope(oldScope), /AUTH_REQUIRED/)
})

test('bridge failures publish only a redacted locked status', async () => {
  const { bridge, coordinator } = fixture(authenticated)
  bridge.status.mockRejectedValue(new Error('agent_history_sessionid=do-not-leak'))
  const events: unknown[] = []
  coordinator.subscribe(status => events.push(status))

  await coordinator.start()

  assert.equal(coordinator.status().state, 'locked')
  assert.equal(coordinator.status().reason, 'runtime_unavailable')
  assert.equal(JSON.stringify(events).includes('sessionid'), false)
})

test('explicit local refresh locks and cleans up before one bridge recovery attempt', async () => {
  const bridge = fixedBridge(authenticated)
  const replacement = fixedBridge({ ...authenticated, runtime_instance_id: 'runtime-2', epoch: 3 })
  const order: string[] = []
  let coordinator: AuthCoordinator

  const cleanup = vi.fn(async () => {
    assert.equal(coordinator.scope('local'), null)
    order.push('cleanup')
  })

  const recoverBridge = vi.fn(async (connectionId, failedBridge) => {
    assert.equal(connectionId, 'local')
    assert.equal(failedBridge, bridge)
    assert.equal(coordinator.scope('local'), null)
    assert.equal(cleanup.mock.calls.length, 1)
    order.push('recover')

    return replacement
  })

  coordinator = new AuthCoordinator(bridge, {
    cleanup,
    clock: () => 42,
    pollIntervalMs: 0,
    recoverBridge
  })
  coordinator.subscribe(status => order.push(`event:${status.state}`))
  await coordinator.start()
  order.length = 0
  bridge.status.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))

  const result = await coordinator.refresh('local', { recoverRuntime: true })

  assert.equal(result.state, 'authenticated')
  assert.equal(coordinator.scope('local')?.runtime_instance_id, 'runtime-2')
  assert.equal(recoverBridge.mock.calls.length, 1)
  assert.equal(replacement.status.mock.calls.length, 1)
  assert.deepEqual(order, ['event:locked', 'cleanup', 'recover', 'event:checking', 'event:authenticated'])
})

test('ordinary refresh remains locked and never rebuilds an unavailable bridge', async () => {
  const bridge = fixedBridge(authenticated)
  const recoverBridge = vi.fn(async () => fixedBridge(authenticated))

  const coordinator = new AuthCoordinator(bridge, {
    clock: () => 42,
    pollIntervalMs: 0,
    recoverBridge
  })

  await coordinator.start()
  bridge.status.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))

  const result = await coordinator.refresh()

  assert.equal(result.state, 'locked')
  assert.equal(result.reason, 'runtime_unavailable')
  assert.equal(coordinator.scope('local'), null)
  assert.equal(recoverBridge.mock.calls.length, 0)
})

test('failed bridge replacement remains locked without a second recovery attempt', async () => {
  const bridge = fixedBridge(authenticated)
  const replacement = fixedBridge(authenticated)
  replacement.status.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))
  const recoverBridge = vi.fn(async () => replacement)

  const coordinator = new AuthCoordinator(bridge, {
    clock: () => 42,
    pollIntervalMs: 0,
    recoverBridge
  })

  await coordinator.start()
  bridge.status.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))

  const result = await coordinator.refresh('local', { recoverRuntime: true })

  assert.equal(result.state, 'locked')
  assert.equal(result.reason, 'runtime_unavailable')
  assert.equal(coordinator.scope('local'), null)
  assert.equal(recoverBridge.mock.calls.length, 1)
  assert.equal(replacement.status.mock.calls.length, 1)
})

test('login failure never rebuilds the bridge or replays the password', async () => {
  const bridge = fixedBridge(signedOut)
  const recoverBridge = vi.fn(async () => fixedBridge(authenticated))
  const coordinator = new AuthCoordinator(bridge, { pollIntervalMs: 0, recoverBridge })
  await coordinator.start()
  bridge.login.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))

  const result = await coordinator.login('alice', 'password-sentinel')

  assert.equal(result.state, 'locked')
  assert.deepEqual(bridge.login.mock.calls, [['alice', 'password-sentinel']])
  assert.equal(recoverBridge.mock.calls.length, 0)
})

test('connection and both policies require exact connection scopes', async () => {
  const { coordinator } = fixture(authenticated)
  await coordinator.start()

  await assert.doesNotReject(coordinator.require('connection', 'local'))
  await assert.doesNotReject(coordinator.require('both', 'local'))
  await assert.rejects(coordinator.require('connection', 'remote-a'), /AUTH_REQUIRED/)
  await assert.rejects(coordinator.require('both', 'remote-a'), /AUTH_REQUIRED/)
})

test('an expired lease locks and cleans up before protected work', async () => {
  let now = 42
  const { cleanup, coordinator } = fixture(authenticated, () => now)
  await coordinator.start()
  now = authenticated.valid_until

  assert.equal(coordinator.isAuthenticated('local'), false)
  await assert.rejects(coordinator.require('local', 'local'), /AUTH_REQUIRED/)
  assert.equal(coordinator.status().state, 'locked')
  assert.equal(coordinator.status().reason, 'session_expired')
  assert.equal(coordinator.scope('local'), null)
  assert.equal(cleanup.mock.calls.length, 1)
})

test('never publishes an authenticated scope when the bridge lease is already expired', async () => {
  const events: BridgeStatus[] = []
  const { coordinator } = fixture({ ...authenticated, valid_until: 42 }, () => 42)
  coordinator.subscribe(status => events.push(status))

  const status = await coordinator.start()

  assert.equal(status.state, 'locked')
  assert.equal(status.reason, 'session_expired')
  assert.equal(coordinator.scope('local'), null)
  assert.equal(events.some(event => event.state === 'authenticated'), false)
})

test('default clock evaluates bridge leases in Unix epoch seconds', async () => {
  const wallNowMs = 1_800_000_000_000
  const dateNow = vi.spyOn(Date, 'now').mockReturnValue(wallNowMs)
  const status = { ...authenticated, valid_until: wallNowMs / 1000 + 60 }
  const coordinator = new AuthCoordinator(fixedBridge(status), { pollIntervalMs: 0 })

  try {
    await coordinator.start()
    assert.equal(coordinator.isAuthenticated(), true)

    dateNow.mockReturnValue(wallNowMs + 60_000)
    assert.equal(coordinator.isAuthenticated(), false)
    await assert.rejects(coordinator.require('local', 'local'), /AUTH_REQUIRED/)
    assert.equal(coordinator.status().reason, 'session_expired')
  } finally {
    coordinator.stop()
    dateNow.mockRestore()
  }
})

test('authorizes local, remote A, remote B, and both policies only from their exact scopes', async () => {
  const local = fixedBridge(statusFor('local-runtime', 1, 'signed_out'))
  const remoteA = fixedBridge(statusFor('remote-a-runtime', 2))
  const remoteB = fixedBridge(statusFor('remote-b-runtime', 3))
  const coordinator = new AuthCoordinator(local, { clock: () => 42, pollIntervalMs: 0 })

  await coordinator.start()
  await coordinator.registerConnection('remote-a', remoteA)
  await coordinator.registerConnection('remote-b', remoteB)

  await assert.rejects(coordinator.require('local', 'local'), /AUTH_REQUIRED/)
  await assert.doesNotReject(coordinator.require('connection', 'remote-a'))
  await assert.doesNotReject(coordinator.require('connection', 'remote-b'))
  await assert.rejects(coordinator.require('connection', 'local'), /AUTH_REQUIRED/)
  await assert.rejects(coordinator.require('both', 'remote-a'), /AUTH_REQUIRED/)

  assert.deepEqual(coordinator.scope('remote-a'), {
    connection_id: 'remote-a',
    runtime_instance_id: 'remote-a-runtime',
    epoch: 2
  })
  assert.deepEqual(coordinator.scope('remote-b'), {
    connection_id: 'remote-b',
    runtime_instance_id: 'remote-b-runtime',
    epoch: 3
  })
})

test('locking or logging out one remote connection does not mutate local or a peer remote', async () => {
  const cleanup = vi.fn(async (_connectionId: string) => {})

  const coordinator = new AuthCoordinator(fixedBridge(statusFor('local-runtime', 1)), {
    cleanup,
    clock: () => 42,
    pollIntervalMs: 0
  })

  const remoteA = fixedBridge(statusFor('remote-a-runtime', 2))
  const remoteB = fixedBridge(statusFor('remote-b-runtime', 3))

  await coordinator.start()
  await coordinator.registerConnection('remote-a', remoteA)
  await coordinator.registerConnection('remote-b', remoteB)
  const localScope = coordinator.scope('local')
  const remoteBScope = coordinator.scope('remote-b')

  await coordinator.logout('remote-a')

  assert.equal(coordinator.scope('remote-a'), null)
  assert.deepEqual(coordinator.scope('local'), localScope)
  assert.deepEqual(coordinator.scope('remote-b'), remoteBScope)
  await assert.doesNotReject(coordinator.require('local', 'local'))
  await assert.doesNotReject(coordinator.require('connection', 'remote-b'))
  await assert.rejects(coordinator.require('connection', 'remote-a'), /AUTH_REQUIRED/)
  assert.deepEqual(cleanup.mock.calls.map(call => call[0]), ['remote-a'])
})

test('replacing a remote bridge revokes its old scope before publishing the new owner', async () => {
  const events: string[] = []

  const coordinator = new AuthCoordinator(fixedBridge(statusFor('local-runtime', 1)), {
    cleanup: async connectionId => {
      assert.equal(coordinator.scope(connectionId), null)
      events.push(`cleanup:${connectionId}`)
    },
    clock: () => 42,
    pollIntervalMs: 0
  })

  coordinator.subscribe((status, connectionId) => events.push(`${connectionId}:${status.state}`))

  await coordinator.start()
  await coordinator.registerConnection('remote-a', fixedBridge(statusFor('remote-a-old', 1)))
  events.length = 0
  await coordinator.registerConnection('remote-a', fixedBridge(statusFor('remote-a-new', 1)))

  assert.deepEqual(events, ['remote-a:locked', 'cleanup:remote-a', 'remote-a:authenticated'])
  assert.equal(coordinator.scope('remote-a')?.runtime_instance_id, 'remote-a-new')
})

test('remote bridge registration cannot replace local or create ambiguous connection ids', async () => {
  const coordinator = new AuthCoordinator(fixedBridge(authenticated), { pollIntervalMs: 0 })

  await assert.rejects(coordinator.registerConnection('local', fixedBridge(authenticated)), /local auth connection/)
  await assert.rejects(coordinator.registerConnection(' remote-a ', fixedBridge(authenticated)), /Invalid/)
})
