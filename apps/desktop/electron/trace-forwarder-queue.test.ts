import assert from 'node:assert/strict'

import { test } from 'vitest'

import { TraceForwarderQueue, type TraceQueueBatch } from './trace-forwarder-queue'

function batch(id: number, epoch = 7, bytes = 4): TraceQueueBatch {
  return {
    body: Buffer.alloc(bytes, id),
    contentType: 'application/x-protobuf',
    entrypoint: 'desktop',
    epoch,
    runId: `run-${id}`,
    sessionId: `session-${id}`,
    telemetrySchemaVersion: '1'
  }
}

test('queue is FIFO and reuses identical bytes without concurrent duplicate sends', async () => {
  let now = 1_000
  let releaseFirst!: () => void

  const firstBlocked = new Promise<void>(resolve => {
    releaseFirst = resolve
  })

  const queue = new TraceForwarderQueue({ clock: () => now, jitter: delay => delay })
  const seen: Buffer[] = []

  const send = async (item: TraceQueueBatch) => {
    seen.push(item.body)

    if (seen.length === 1) {
      await firstBlocked

      return 'retry' as const
    }

    return 'sent' as const
  }

  queue.activateEpoch(7)
  queue.enqueue(batch(1))
  queue.enqueue(batch(2))
  const firstPump = queue.pump(send)
  const overlappingPump = queue.pump(send)

  releaseFirst()
  await Promise.all([firstPump, overlappingPump])
  assert.equal(seen.length, 1)
  const firstBytes = seen[0]

  now += 1_000
  await queue.pump(send)

  assert.equal(seen[1], firstBytes)
  assert.deepEqual(seen.map(body => body[0]), [1, 1, 2])
  assert.equal(queue.summary().queued, 0)
})

test('queue drops oldest batches at 128 items or 32 MiB and never persists them', () => {
  const queue = new TraceForwarderQueue({
    clock: () => 1_000,
    maxBytes: 12,
    maxItems: 3
  })

  queue.activateEpoch(7)
  queue.enqueue(batch(1))
  queue.enqueue(batch(2))
  queue.enqueue(batch(3))
  queue.enqueue(batch(4))
  queue.enqueue(batch(5, 7, 8))

  assert.deepEqual(queue.inspectForTest().map(item => item.body[0]), [4, 5])
  assert.deepEqual(queue.summary(), {
    discarded: 0,
    dropped: 3,
    expired: 0,
    queued: 2,
    queuedBytes: 12,
    retried: 0,
    sent: 0
  })
  assert.equal(queue.enqueue(batch(6, 7, 13)).accepted, false)
  assert.equal(queue.summary().dropped, 4)
})

test('queue retries at 1/2/4/8/16/30 seconds and expires after 15 minutes', async () => {
  let now = 10_000
  const queue = new TraceForwarderQueue({ clock: () => now, jitter: delay => delay })
  const attempts: number[] = []

  queue.activateEpoch(7)
  queue.enqueue(batch(1))

  for (const delay of [1, 2, 4, 8, 16, 30, 30]) {
    await queue.pump(async () => {
      attempts.push(now)

      return 'retry'
    })
    assert.equal(queue.nextRetryAt(), now + delay * 1_000)
    now += delay * 1_000 - 1
    await queue.pump(async () => assert.fail('backoff must prevent an early retry'))
    now += 1
  }

  assert.equal(attempts.length, 7)
  now = 10_000 + 15 * 60 * 1_000
  await queue.pump(async () => assert.fail('expired batches must not be sent'))
  assert.equal(queue.summary().expired, 1)
  assert.equal(queue.summary().queued, 0)
})

test('changing auth epoch atomically discards every prior-user batch', async () => {
  const queue = new TraceForwarderQueue({ clock: () => 1_000 })

  queue.activateEpoch(7)
  queue.enqueue(batch(1, 7))
  queue.enqueue(batch(2, 7))
  queue.activateEpoch(8)
  queue.enqueue(batch(3, 8))
  const seen: number[] = []
  await queue.pump(async item => {
    seen.push(item.body[0])

    return 'sent'
  })

  assert.deepEqual(seen, [3])
  assert.equal(queue.summary().discarded, 2)
})
