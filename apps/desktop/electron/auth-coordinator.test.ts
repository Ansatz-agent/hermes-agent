import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { AuthBridgeError, type BridgeStatus } from './auth-bridge'
import { AuthCoordinator, CoordinatorAuthRequiredError } from './auth-coordinator'

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
  valid_until: 90,
  account_id: '22222222-2222-4222-8222-222222222222',
  session_id: '33333333-3333-4333-8333-333333333333',
  installation_id: '11111111-1111-4111-8111-111111111111',
  principal_key: 'account:22222222-2222-4222-8222-222222222222',
  validation_state: 'online',
  last_validated_at: '2026-08-24T12:00:00+00:00',
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
    logout: vi.fn(
      async (): Promise<BridgeStatus> => ({
        ...status,
        state: 'signed_out',
        username: null,
        valid_until: 0
      })
    )
  }
}

function terminalStatus(reason: 'account_disabled' | 'account_revoked' | 'session_revoked'): BridgeStatus {
  return {
    ...authenticated,
    state: 'locked',
    username: null,
    valid_until: 0,
    validation_state: 'degraded',
    validation_reason: reason,
    reason
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

test('requiring the current scope does not refresh or rebroadcast authentication', async () => {
  const { bridge, coordinator } = fixture(authenticated)
  const events: BridgeStatus[] = []
  coordinator.subscribe(status => events.push(status))
  await coordinator.start()
  events.length = 0

  const scope = await coordinator.requireCurrentScope('local')

  assert.deepEqual(scope, {
    connection_id: 'local',
    runtime_instance_id: 'runtime-1',
    epoch: 2
  })
  assert.equal(bridge.status.mock.calls.length, 1)
  assert.deepEqual(events, [])
})

test('retains local scope for a finite degraded cached native status', async () => {
  const cachedNative = {
    ...authenticated,
    valid_until: 253_402_300_799,
    validation_state: 'degraded' as const,
    validation_reason: 'server_unavailable' as const
  }

  const coordinator = new AuthCoordinator(fixedBridge(cachedNative), {
    clock: () => 1_800_000_000,
    pollIntervalMs: 0
  })

  await coordinator.start()

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

test('logout recovers an unavailable local bridge and finishes signed out', async () => {
  const bridge = fixedBridge(authenticated)
  const replacement = fixedBridge(authenticated)
  replacement.logout.mockResolvedValue(signedOut)

  const cleanup = vi.fn(async () => {})
  const recoverBridge = vi.fn(async () => replacement)
  const coordinator = new AuthCoordinator(bridge, {
    cleanup,
    pollIntervalMs: 0,
    recoverBridge
  })

  await coordinator.start()
  bridge.logout.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))

  const result = await coordinator.logout()

  assert.equal(result.state, 'signed_out')
  assert.equal(result.reason, 'signed_out')
  assert.equal(coordinator.status().state, 'signed_out')
  assert.equal(coordinator.scope('local'), null)
  assert.equal(cleanup.mock.calls.length, 1)
  assert.deepEqual(recoverBridge.mock.calls, [['local', bridge]])
  assert.equal(replacement.logout.mock.calls.length, 1)
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

test.each([
  'server_unavailable',
  'rate_limited',
  'invalid_response',
  'invalid_session_credential',
  'runtime_unavailable'
])('preserves local scope and authorization for transient %s', async reason => {
  const { bridge, cleanup, coordinator } = fixture(authenticated)
  await coordinator.start()
  const before = coordinator.scope('local')

  bridge.status.mockRejectedValueOnce(new AuthBridgeError(reason, reason))
  const result = await coordinator.refresh('local', { recoverRuntime: true })

  assert.deepEqual(coordinator.scope('local'), before)
  assert.equal(result.state, 'authenticated')
  assert.equal(result.validation_state, 'degraded')
  assert.equal(result.validation_reason, reason)
  assert.equal(cleanup.mock.calls.length, 0)
  await assert.doesNotReject(coordinator.require('local', 'local'))
})

test('redacts an unknown bridge failure while preserving local authorization', async () => {
  const { bridge, cleanup, coordinator } = fixture(authenticated)
  const events: unknown[] = []
  coordinator.subscribe(status => events.push(status))

  await coordinator.start()
  bridge.status.mockRejectedValueOnce(new Error('agent_history_sessionid=do-not-leak'))
  await coordinator.refresh()

  assert.equal(coordinator.status().state, 'authenticated')
  assert.equal(coordinator.status().validation_state, 'degraded')
  assert.equal(coordinator.status().validation_reason, 'runtime_unavailable')
  assert.equal(cleanup.mock.calls.length, 0)
  await assert.doesNotReject(coordinator.require('local', 'local'))
  assert.equal(JSON.stringify(events).includes('sessionid'), false)
})

test('local bridge recovery preserves the original scope without cleanup', async () => {
  const bridge = fixedBridge(authenticated)
  const replacement = fixedBridge({ ...authenticated, runtime_instance_id: 'runtime-2', epoch: 3 })
  const order: string[] = []
  let coordinator: AuthCoordinator

  const cleanup = vi.fn(async () => {
    assert.fail('transient local recovery must not clean the existing scope')
    order.push('cleanup')
  })

  const recoverBridge = vi.fn(async (connectionId, failedBridge) => {
    assert.equal(connectionId, 'local')
    assert.equal(failedBridge, bridge)
    assert.equal(coordinator.scope('local')?.runtime_instance_id, 'runtime-1')
    assert.equal(cleanup.mock.calls.length, 0)
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
  assert.equal(coordinator.scope('local')?.runtime_instance_id, 'runtime-1')
  assert.equal(recoverBridge.mock.calls.length, 1)
  assert.equal(replacement.status.mock.calls.length, 1)
  assert.deepEqual(order, ['event:authenticated', 'recover', 'event:authenticated', 'event:authenticated'])
})

test('ordinary local refresh degrades without rebuilding an unavailable bridge', async () => {
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

  assert.equal(result.state, 'authenticated')
  assert.equal(result.validation_state, 'degraded')
  assert.equal(result.validation_reason, 'runtime_unavailable')
  assert.notEqual(coordinator.scope('local'), null)
  assert.equal(recoverBridge.mock.calls.length, 0)
})

test('failed bridge replacement remains degraded without a second recovery attempt', async () => {
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

  assert.equal(result.state, 'authenticated')
  assert.equal(result.validation_state, 'degraded')
  assert.equal(result.validation_reason, 'runtime_unavailable')
  assert.notEqual(coordinator.scope('local'), null)
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

test('login can recover an unavailable local bridge before submitting the password once', async () => {
  const bridge = fixedBridge(signedOut)
  const replacement = fixedBridge(signedOut)
  const recoverBridge = vi.fn(async () => replacement)
  const coordinator = new AuthCoordinator(bridge, { pollIntervalMs: 0, recoverBridge })
  await coordinator.start()
  bridge.status.mockRejectedValueOnce(new AuthBridgeError('runtime_unavailable', 'runtime_unavailable'))
  replacement.login.mockResolvedValueOnce(authenticated)

  const result = await coordinator.login('alice', 'password-sentinel', 'local', {
    recoverRuntimeBeforeSubmit: true
  })

  assert.equal(result.state, 'authenticated')
  assert.equal(bridge.login.mock.calls.length, 0)
  assert.deepEqual(replacement.login.mock.calls, [['alice', 'password-sentinel']])
  assert.deepEqual(recoverBridge.mock.calls, [['local', bridge]])
})

test('connection and both policies require exact connection scopes', async () => {
  const { coordinator } = fixture(authenticated)
  await coordinator.start()

  await assert.doesNotReject(coordinator.require('connection', 'local'))
  await assert.doesNotReject(coordinator.require('both', 'local'))
  await assert.rejects(coordinator.require('connection', 'remote-a'), /AUTH_REQUIRED/)
  await assert.rejects(coordinator.require('both', 'remote-a'), /AUTH_REQUIRED/)
})

test('a local native authorization is not revoked by its bridge lease expiry', async () => {
  let now = 42
  const { cleanup, coordinator } = fixture(authenticated, () => now)
  await coordinator.start()
  now = authenticated.valid_until

  assert.equal(coordinator.isAuthenticated('local'), true)
  await assert.doesNotReject(coordinator.require('local', 'local'))
  assert.equal(coordinator.status().state, 'authenticated')
  assert.notEqual(coordinator.scope('local'), null)
  assert.equal(cleanup.mock.calls.length, 0)
})

test('publishes a locally authorized native scope when its bridge lease is already expired', async () => {
  const events: BridgeStatus[] = []
  const { coordinator } = fixture({ ...authenticated, valid_until: 42 }, () => 42)
  coordinator.subscribe(status => events.push(status))

  const status = await coordinator.start()

  assert.equal(status.state, 'authenticated')
  assert.notEqual(coordinator.scope('local'), null)
  assert.equal(
    events.some(event => event.state === 'authenticated'),
    true
  )
})

test('default clock does not expire a locally authorized native principal', async () => {
  const wallNowMs = 1_800_000_000_000
  const dateNow = vi.spyOn(Date, 'now').mockReturnValue(wallNowMs)
  const status = { ...authenticated, valid_until: wallNowMs / 1000 + 60 }
  const coordinator = new AuthCoordinator(fixedBridge(status), { pollIntervalMs: 0 })

  try {
    await coordinator.start()
    assert.equal(coordinator.isAuthenticated(), true)

    dateNow.mockReturnValue(wallNowMs + 60_000)
    assert.equal(coordinator.isAuthenticated(), true)
    await assert.doesNotReject(coordinator.require('local', 'local'))
  } finally {
    coordinator.stop()
    dateNow.mockRestore()
  }
})

test.each(['account_disabled', 'account_revoked', 'session_revoked'] as const)(
  'matching current %s removes local scope and cleans exactly once',
  async reason => {
    const { cleanup, coordinator, setStatus } = fixture(authenticated)
    const terminalEvents: BridgeStatus[] = []
    coordinator.subscribe(status => {
      if (status.reason === reason) {
        terminalEvents.push(status)
      }
    })
    await coordinator.start()
    setStatus(terminalStatus(reason))

    const first = await coordinator.refresh()
    const second = await coordinator.refresh()

    assert.equal(first.reason, reason)
    assert.equal(second.reason, reason)
    assert.equal(coordinator.scope('local'), null)
    assert.equal(cleanup.mock.calls.length, 1)
    assert.equal(terminalEvents.length, 1)
    await assert.rejects(coordinator.require('local', 'local'), /AUTH_REQUIRED/)
  }
)

test.each(['account_disabled', 'account_revoked'] as const)(
  'matching current %s removes local scope even when its status carries an older Session id',
  async reason => {
    const { cleanup, coordinator, setStatus } = fixture(authenticated)
    await coordinator.start()
    setStatus({ ...terminalStatus(reason), session_id: 'old-session' })

    await coordinator.refresh()

    assert.equal(coordinator.scope('local'), null)
    assert.equal(cleanup.mock.calls.length, 1)
    await assert.rejects(coordinator.require('local', 'local'), /AUTH_REQUIRED/)
  }
)

test('a stale session_revoked event from the same account does not clean the newer local Session', async () => {
  const { cleanup, coordinator, setStatus } = fixture(authenticated)
  await coordinator.start()
  setStatus({ ...authenticated, session_id: 'new-session' })
  await coordinator.refresh()
  setStatus({ ...terminalStatus('session_revoked'), session_id: 'old-session' })

  const result = await coordinator.refresh()

  assert.equal(result.state, 'authenticated')
  assert.equal(result.session_id, 'new-session')
  assert.equal(coordinator.scope('local')?.runtime_instance_id, 'runtime-1')
  assert.equal(cleanup.mock.calls.length, 0)
})

test('matching Trace terminal revocation locks and cleans exactly once while mismatches are ignored', async () => {
  const { cleanup, coordinator } = fixture(authenticated)
  await coordinator.start()

  assert.equal(
    await coordinator.applyTraceTerminalRevocation({
      accountId: authenticated.account_id!,
      code: 'session_revoked',
      revokedAt: '2026-08-25T00:00:00Z',
      sessionId: '44444444-4444-4444-8444-444444444444'
    }),
    false
  )
  assert.equal(coordinator.isAuthenticated(), true)

  const revocation = {
    accountId: authenticated.account_id!,
    code: 'session_revoked' as const,
    revokedAt: '2026-08-25T00:00:00Z',
    sessionId: authenticated.session_id!
  }
  assert.equal(await coordinator.applyTraceTerminalRevocation(revocation), true)
  assert.equal(await coordinator.applyTraceTerminalRevocation(revocation), false)
  assert.equal(coordinator.status().reason, 'session_revoked')
  assert.equal(coordinator.isAuthenticated(), false)
  assert.equal(cleanup.mock.calls.length, 1)
})

test('a mismatched terminal identity degrades without replacing or cleaning the current account', async () => {
  const { cleanup, coordinator, setStatus } = fixture(authenticated)
  await coordinator.start()
  const before = coordinator.scope('local')
  setStatus({
    ...terminalStatus('session_revoked'),
    account_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    session_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    principal_key: 'account:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  })

  const result = await coordinator.refresh()

  assert.equal(result.state, 'authenticated')
  assert.equal(result.validation_state, 'degraded')
  assert.equal(result.validation_reason, 'session_revoked')
  assert.deepEqual(coordinator.scope('local'), before)
  assert.equal(cleanup.mock.calls.length, 0)
  await assert.doesNotReject(coordinator.require('local', 'local'))
})

test('a true account switch cleans the prior scope once and publishes the new scope', async () => {
  const { cleanup, coordinator, setStatus } = fixture(authenticated)
  await coordinator.start()
  setStatus({
    ...authenticated,
    username: 'bob',
    account_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    session_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    principal_key: 'account:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    runtime_instance_id: 'runtime-2',
    epoch: 3
  })

  await coordinator.refresh()

  assert.deepEqual(coordinator.scope('local'), {
    connection_id: 'local',
    runtime_instance_id: 'runtime-2',
    epoch: 3
  })
  assert.equal(cleanup.mock.calls.length, 1)
  await assert.doesNotReject(coordinator.require('local', 'local'))
})

test('an older terminal result cannot clean a newer authenticated account', async () => {
  const { cleanup, coordinator, setStatus } = fixture(authenticated)
  await coordinator.start()
  const oldTerminal = terminalStatus('session_revoked')
  setStatus({
    ...authenticated,
    username: 'bob',
    account_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    session_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    principal_key: 'account:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    runtime_instance_id: 'runtime-2',
    epoch: 3
  })
  await coordinator.refresh()
  const cleanupAfterSwitch = cleanup.mock.calls.length
  setStatus(oldTerminal)

  const result = await coordinator.refresh()

  assert.equal(result.state, 'authenticated')
  assert.equal(result.principal_key, 'account:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')
  assert.equal(coordinator.scope('local')?.runtime_instance_id, 'runtime-2')
  assert.equal(cleanup.mock.calls.length, cleanupAfterSwitch)
})

test('an expired remote legacy lease remains isolated from local native authorization', async () => {
  const local = fixedBridge(authenticated)

  const remote = fixedBridge({
    ...authenticated,
    legacy: true,
    valid_until: 42,
    runtime_instance_id: 'remote-runtime',
    epoch: 4
  })

  const cleanup = vi.fn(async () => {})
  const coordinator = new AuthCoordinator(local, { cleanup, clock: () => 42, pollIntervalMs: 0 })
  await coordinator.start()
  await coordinator.registerConnection('remote-a', remote)

  assert.notEqual(coordinator.scope('local'), null)
  assert.equal(coordinator.scope('remote-a'), null)
  assert.equal(cleanup.mock.calls.length, 0)
  await assert.doesNotReject(coordinator.require('local', 'local'))
  await assert.rejects(coordinator.require('connection', 'remote-a'), /AUTH_REQUIRED/)
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
  assert.deepEqual(
    cleanup.mock.calls.map(call => call[0]),
    ['remote-a']
  )
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
