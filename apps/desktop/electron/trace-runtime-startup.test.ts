import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveLocalBackendWithTrace, TraceRuntimeStartupRecovery } from './trace-runtime-startup'

function fakeTimers() {
  let nextId = 0
  const callbacks = new Map<number, { delay: number; run: () => void }>()

  return {
    clear(id: number) {
      callbacks.delete(id)
    },
    fire(delay: number) {
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

async function nextTurn() {
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

test.each(['safeStorage unavailable', 'outbox permission denied', 'journal corrupt', 'listener unavailable'])(
  'Trace %s enters encrypted degraded recovery without blocking local backend resolution',
  async failure => {
    const timers = fakeTimers()
    const events: string[] = []
    let attempts = 0
    let plaintextFallbacks = 0
    const recovery = new TraceRuntimeStartupRecovery({ clearTimer: timers.clear, setTimer: timers.set })

    const backend = await resolveLocalBackendWithTrace({
      key: 'legacy-' + 'a'.repeat(64),
      recovery,
      resolveBackend: () => {
        events.push('backend')

        return { kind: 'local' as const }
      },
      startEncryptedTrace: async () => {
        attempts += 1
        events.push(`trace-${attempts}`)

        if (attempts === 1) {
          throw new Error(failure)
        }

        return { encrypted: true }
      }
    })

    assert.deepEqual(backend, { kind: 'local' })
    assert.deepEqual(events, ['trace-1', 'backend'])
    assert.equal(recovery.state(), 'degraded')
    assert.equal(plaintextFallbacks, 0)

    timers.fire(1_000)
    await nextTurn()
    assert.equal(attempts, 2)
    assert.equal(recovery.state(), 'ready')
    assert.equal(plaintextFallbacks, 0)
    await recovery.stop()
  }
)

test('Trace degraded retry uses bounded exponential delays and stop clears pending recovery', async () => {
  const timers = fakeTimers()
  let attempts = 0
  const recovery = new TraceRuntimeStartupRecovery({ clearTimer: timers.clear, setTimer: timers.set })

  const startEncryptedTrace = async () => {
    attempts += 1
    throw new Error('disk unavailable')
  }

  await recovery.prepare('legacy-' + 'b'.repeat(64), startEncryptedTrace)
  timers.fire(1_000)
  await nextTurn()
  timers.fire(2_000)
  await nextTurn()

  assert.equal(attempts, 3)
  assert.equal(timers.pending(), 1)
  await recovery.stop()
  assert.equal(timers.pending(), 0)
})
