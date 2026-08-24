import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  ALL_TRACE_RECOVERY_REASONS,
  legacyTraceOwner,
  TraceRecoveryController,
  TraceRecoveryLifecycle
} from './trace-recovery-controller'

function deferred() {
  let resolve!: () => void

  const promise = new Promise<void>(currentResolve => {
    resolve = currentResolve
  })

  return { promise, resolve }
}

async function nextTurn() {
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

test('coalesces same-turn recovery signals into one account pump', async () => {
  const gate = deferred()
  let active = 0
  let maxActive = 0
  let pumps = 0

  const controller = new TraceRecoveryController({
    accountKey: `account-11111111-1111-4111-8111-111111111111`,
    pump: async () => {
      pumps += 1
      active += 1
      maxActive = Math.max(maxActive, active)
      await gate.promise
      active -= 1
    }
  })

  for (const reason of ALL_TRACE_RECOVERY_REASONS) {
    controller.trigger(reason)
  }

  await nextTurn()
  assert.equal(pumps, 1)
  assert.equal(maxActive, 1)

  gate.resolve()
  await controller.whenIdle()
  assert.equal(pumps, 1)
})

test('runs one follow-up pump when a signal arrives after the current pump began', async () => {
  const first = deferred()
  const second = deferred()
  let active = 0
  let maxActive = 0
  let pumps = 0

  const controller = new TraceRecoveryController({
    accountKey: `legacy-${'a'.repeat(64)}`,
    pump: async () => {
      const current = ++pumps
      active += 1
      maxActive = Math.max(maxActive, active)
      await (current === 1 ? first.promise : second.promise)
      active -= 1
    }
  })

  controller.trigger('startup')
  await nextTurn()
  assert.equal(pumps, 1)

  controller.trigger('renderer-online')
  controller.trigger('focus')
  first.resolve()
  await nextTurn()

  assert.equal(pumps, 2)
  assert.equal(maxActive, 1)

  second.resolve()
  await controller.whenIdle()
})

test('stop closes admission without running queued or future recovery work', async () => {
  const gate = deferred()
  let pumps = 0

  const controller = new TraceRecoveryController({
    accountKey: `legacy-${'b'.repeat(64)}`,
    pump: async () => {
      pumps += 1
      await gate.promise
    }
  })

  controller.trigger('startup')
  await nextTurn()
  const stopped = controller.stop()
  controller.trigger('resume')
  gate.resolve()
  await stopped
  controller.trigger('timer')
  await nextTurn()

  assert.equal(pumps, 1)
})

test('rejects an account namespace that cannot own a Trace outbox', () => {
  assert.throws(
    () => new TraceRecoveryController({ accountKey: '../escape', pump: async () => {} }),
    /invalid_account_key/
  )
})

test('legacy principal digests create validated local-only owners', () => {
  const owner = legacyTraceOwner('c'.repeat(64), '11111111-1111-4111-8111-111111111111')

  assert.deepEqual(owner, {
    accountId: null,
    accountKey: `legacy-${'c'.repeat(64)}`,
    installationId: '11111111-1111-4111-8111-111111111111',
    sessionId: null
  })
  assert.throws(() => legacyTraceOwner('../escape', '11111111-1111-4111-8111-111111111111'), /invalid_account_key/)
})

function fakeTimers() {
  let nextId = 0
  const callbacks = new Map<number, { delay: number; run: () => void }>()

  return {
    clear(id: number) {
      callbacks.delete(id)
    },
    fireDelay(delay: number) {
      const entry = [...callbacks.entries()].find(([, timer]) => timer.delay === delay)

      assert.ok(entry, `missing ${delay} ms timer`)
      callbacks.delete(entry[0])
      entry[1].run()
    },
    pending: () => callbacks.size,
    set(run: () => void, delay: number) {
      const id = ++nextId
      callbacks.set(id, { delay, run })

      return id
    }
  }
}

test('lifecycle emits startup, token, network, power, focus, and periodic recovery signals', async () => {
  const reasons: string[] = []
  const token = deferred()
  const timers = fakeTimers()
  const now = 1_000_000
  let tokenCalls = 0

  const lifecycle = new TraceRecoveryLifecycle({
    clearTimer: timers.clear,
    clock: () => now,
    controller: { trigger: reason => reasons.push(reason) },
    credentialProvider: {
      current: async () => {
        tokenCalls += 1
        await token.promise
      },
      expiresAt: () => now + 120_000
    },
    periodicMs: 30_000,
    setTimer: timers.set
  })

  lifecycle.start()
  lifecycle.trigger('renderer-online')
  lifecycle.trigger('resume')
  lifecycle.trigger('focus')
  assert.deepEqual(reasons, ['startup', 'renderer-online', 'resume', 'focus'])
  assert.equal(tokenCalls, 1)

  token.resolve()
  await nextTurn()
  assert.deepEqual(reasons, ['startup', 'renderer-online', 'resume', 'focus', 'token-ready'])

  timers.fireDelay(60_000)
  await nextTurn()
  assert.equal(reasons.includes('token-near-expiry'), true)
  assert.equal(tokenCalls, 2)

  timers.fireDelay(30_000)
  assert.equal(reasons.includes('timer'), true)
  await lifecycle.stop()
  assert.equal(timers.pending(), 0)
})

test('lifecycle schedules the exact retry due time and cancels it on stop', async () => {
  const reasons: string[] = []
  const timers = fakeTimers()
  let now = 2_000

  const lifecycle = new TraceRecoveryLifecycle({
    clearTimer: timers.clear,
    clock: () => now,
    controller: { trigger: reason => reasons.push(reason) },
    credentialProvider: {
      current: async () => {},
      expiresAt: () => null
    },
    periodicMs: 30_000,
    setTimer: timers.set
  })

  lifecycle.scheduleRetryAt(7_000)
  timers.fireDelay(5_000)
  assert.deepEqual(reasons, ['timer'])

  now = 7_000
  lifecycle.scheduleRetryAt(9_000)
  await lifecycle.stop()
  assert.equal(timers.pending(), 0)
})

test('lifecycle cleanup does not wait for a stopped pump that is still unwinding', async () => {
  const pumpStopped = deferred()
  let stopSettled = false

  const lifecycle = new TraceRecoveryLifecycle({
    controller: {
      stop: () => pumpStopped.promise,
      trigger: () => {}
    },
    credentialProvider: {
      current: async () => {},
      expiresAt: () => null
    }
  })

  const stopped = lifecycle.stop().then(() => {
    stopSettled = true
  })

  await nextTurn()
  assert.equal(stopSettled, true)

  pumpStopped.resolve()
  await stopped
})
