import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { BridgeStatus } from './auth-bridge'
import { runAuthenticatedRuntimePreparation } from './authenticated-runtime-preparation'

function authenticatedStatus(overrides: Partial<BridgeStatus> = {}): BridgeStatus {
  return {
    state: 'authenticated',
    username: 'alice',
    account_id: '22222222-2222-4222-8222-222222222222',
    session_id: '33333333-3333-4333-8333-333333333333',
    installation_id: '11111111-1111-4111-8111-111111111111',
    principal_key: 'account:22222222-2222-4222-8222-222222222222',
    runtime_instance_id: 'runtime-a',
    epoch: 7,
    valid_until: 60,
    validation_state: 'online',
    validation_reason: null,
    last_validated_at: '2026-08-24T12:00:00+00:00',
    legacy: false,
    reason: null,
    principal_key: `legacy:${'a'.repeat(64)}`,
    ...overrides
  }
}

function signedOutStatus(overrides: Partial<BridgeStatus> = {}): BridgeStatus {
  return {
    state: 'signed_out',
    username: null,
    account_id: null,
    session_id: null,
    installation_id: null,
    principal_key: null,
    runtime_instance_id: 'runtime-a',
    epoch: 8,
    valid_until: 0,
    validation_state: 'unknown',
    validation_reason: null,
    last_validated_at: null,
    legacy: false,
    reason: 'signed_out',
    principal_key: null,
    ...overrides
  }
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

test('does not start preparation when the observed Session is already stale', async () => {
  const observedStatus = authenticatedStatus()
  let prepareCalls = 0

  const result = await runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: async () => {
      prepareCalls += 1
    },
    currentStatus: () => signedOutStatus(),
    isAuthenticated: () => false,
    onCurrentReady: () => assert.fail('stale preparation must not become ready'),
    onCurrentFailure: () => assert.fail('stale preparation must not report failure')
  })

  assert.equal(result, 'stale')
  assert.equal(prepareCalls, 0)
})

test('suppresses a rejected preparation after logout', async () => {
  const observedStatus = authenticatedStatus()
  const preparation = deferred()
  let currentStatus: BridgeStatus = observedStatus
  let authenticated = true
  let readyCalls = 0
  let failureCalls = 0

  const pending = runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: () => preparation.promise,
    currentStatus: () => currentStatus,
    isAuthenticated: () => authenticated,
    onCurrentReady: () => {
      readyCalls += 1
    },
    onCurrentFailure: () => {
      failureCalls += 1
    }
  })

  authenticated = false
  currentStatus = signedOutStatus()
  preparation.reject(Object.assign(new Error('AUTH_REQUIRED'), { code: 'AUTH_REQUIRED' }))

  assert.equal(await pending, 'stale')
  assert.equal(readyCalls, 0)
  assert.equal(failureCalls, 0)
})

test('suppresses a successful preparation after logout', async () => {
  const observedStatus = authenticatedStatus()
  const preparation = deferred()
  let currentStatus: BridgeStatus = observedStatus
  let authenticated = true
  let readyCalls = 0

  const pending = runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: () => preparation.promise,
    currentStatus: () => currentStatus,
    isAuthenticated: () => authenticated,
    onCurrentReady: () => {
      readyCalls += 1
    },
    onCurrentFailure: () => assert.fail('stale success must not report failure')
  })

  authenticated = false
  currentStatus = signedOutStatus()
  preparation.resolve()

  assert.equal(await pending, 'stale')
  assert.equal(readyCalls, 0)
})

test('suppresses old success after a replacement runtime instance authenticates', async () => {
  const observedStatus = authenticatedStatus()
  const preparation = deferred()
  let currentStatus: BridgeStatus = observedStatus
  let readyCalls = 0

  const pending = runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: () => preparation.promise,
    currentStatus: () => currentStatus,
    isAuthenticated: () => true,
    onCurrentReady: () => {
      readyCalls += 1
    },
    onCurrentFailure: () => assert.fail('replacement Session must not receive old failure')
  })

  currentStatus = authenticatedStatus({ runtime_instance_id: 'runtime-b', epoch: 1 })
  preparation.resolve()

  assert.equal(await pending, 'stale')
  assert.equal(readyCalls, 0)
})

test('suppresses old failure after a replacement epoch authenticates', async () => {
  const observedStatus = authenticatedStatus()
  const preparation = deferred()
  let currentStatus: BridgeStatus = observedStatus
  let failureCalls = 0

  const pending = runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: () => preparation.promise,
    currentStatus: () => currentStatus,
    isAuthenticated: () => true,
    onCurrentReady: () => assert.fail('failed preparation must not become ready'),
    onCurrentFailure: () => {
      failureCalls += 1
    }
  })

  currentStatus = authenticatedStatus({ epoch: observedStatus.epoch + 1 })
  preparation.reject(new Error('AUTH_REQUIRED'))

  assert.equal(await pending, 'stale')
  assert.equal(failureCalls, 0)
})

test('publishes successful readiness once with freshly read current status', async () => {
  const observedStatus = authenticatedStatus()
  const currentStatus = authenticatedStatus({ username: 'renamed-display-value', valid_until: 120 })
  const readyStatuses: BridgeStatus[] = []

  const result = await runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: async () => {},
    currentStatus: () => currentStatus,
    isAuthenticated: () => true,
    onCurrentReady: status => {
      readyStatuses.push(status)
    },
    onCurrentFailure: () => assert.fail('current success must not report failure')
  })

  assert.equal(result, 'ready')
  assert.deepEqual(readyStatuses, [currentStatus])
})

test('publishes a genuine current failure exactly once with fresh status', async () => {
  const observedStatus = authenticatedStatus()
  const currentStatus = authenticatedStatus({ valid_until: 120 })
  const failure = new Error('runtime unavailable')
  const failures: Array<{ error: unknown; status: BridgeStatus }> = []

  const result = await runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: async () => Promise.reject(failure),
    currentStatus: () => currentStatus,
    isAuthenticated: () => true,
    onCurrentReady: () => assert.fail('failed preparation must not become ready'),
    onCurrentFailure: (error, status) => {
      failures.push({ error, status })
    }
  })

  assert.equal(result, 'current-failure')
  assert.deepEqual(failures, [{ error: failure, status: currentStatus }])
})

test('suppresses a ready callback failure that settles after logout', async () => {
  const observedStatus = authenticatedStatus()
  const readyWork = deferred()
  let currentStatus: BridgeStatus = observedStatus
  let authenticated = true
  let readyCalls = 0
  let failureCalls = 0

  const pending = runAuthenticatedRuntimePreparation({
    observedStatus,
    prepare: async () => {},
    currentStatus: () => currentStatus,
    isAuthenticated: () => authenticated,
    onCurrentReady: async () => {
      readyCalls += 1
      await readyWork.promise
    },
    onCurrentFailure: () => {
      failureCalls += 1
    }
  })

  await Promise.resolve()
  authenticated = false
  currentStatus = signedOutStatus()
  readyWork.reject(Object.assign(new Error('AUTH_REQUIRED'), { code: 'AUTH_REQUIRED' }))

  assert.equal(await pending, 'stale')
  assert.equal(readyCalls, 1)
  assert.equal(failureCalls, 0)
})
