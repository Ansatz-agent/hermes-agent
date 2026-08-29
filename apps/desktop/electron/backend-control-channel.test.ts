import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'

import { afterEach, test, vi } from 'vitest'

import { BackendControlChannel, type ChildProcessLike } from './backend-control-channel'

const REGISTRATION_ID = 'UlJSUlJSUlJSUlJSUlJSUg'
const TRANSITION_ID = 'VFRUVFRUVFRUVFRUVFRUVA'

type FakeChild = ChildProcessLike &
  EventEmitter & {
    stdin: PassThrough
    stdout: PassThrough
  }

function fakeChild(): FakeChild {
  const child = new EventEmitter() as FakeChild
  child.stdin = new PassThrough()
  child.stdout = new PassThrough()

  return child
}

function registeredAck(registrationId = REGISTRATION_ID) {
  return {
    version: 2 as const,
    operation: 'scope_token_registered' as const,
    registration_id: registrationId,
    connection_id: 'local',
    runtime_instance_id: '0123456789abcdef0123456789abcdef',
    epoch: 7,
    ttl_seconds: 1_800
  }
}

function promotedAck(transitionId = TRANSITION_ID) {
  return {
    version: 2 as const,
    operation: 'scope_token_promoted' as const,
    transition_id: transitionId,
    registration_id: REGISTRATION_ID,
    previous_registration_id: null,
    connection_id: 'local',
    runtime_instance_id: '0123456789abcdef0123456789abcdef',
    epoch: 7,
    overlap_seconds: 60
  }
}

afterEach(() => {
  vi.useRealTimers()
})

test('routes split ready and ACK lines without leaking control payload to logs', async () => {
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })
  const ready = channel.waitForReady({ timeoutMs: 1_000 })
  const ack = channel.expectAck(value => value.operation === 'scope_token_registered', 1_000)
  const encodedAck = JSON.stringify(registeredAck())

  child.stdout.write('normal log\nHERMES_BACKEND_READY port=')
  child.stdout.write('54321 desktop_scope_protocol=2\nANSATZ_SCOPE_CONTROL_V2 ')
  child.stdout.write(`${encodedAck.slice(0, 35)}`)
  child.stdout.write(`${encodedAck.slice(35)}\n`)

  assert.deepEqual(await ready, { port: 54_321, desktopScopeProtocol: 2 })
  assert.deepEqual(await ack, registeredAck())
  assert.deepEqual(logs, ['normal log'])
  channel.close()
})

test('preserves UTF-8 log characters split across stdout chunks', () => {
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })
  const encoded = Buffer.from('模型日志\n', 'utf8')

  child.stdout.write(encoded.subarray(0, 1))
  child.stdout.write(encoded.subarray(1, 5))
  child.stdout.write(encoded.subarray(5))

  assert.deepEqual(logs, ['模型日志'])
  channel.close()
})

test('accepts CRLF framing while stripping carriage returns from ordinary logs', async () => {
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })
  const ready = channel.waitForReady({ timeoutMs: 1_000 })
  const ack = channel.expectAck(value => value.operation === 'scope_token_registered', 1_000)

  child.stdout.write('ordinary log\r\n')
  child.stdout.write('HERMES_BACKEND_READY port=54321 desktop_scope_protocol=2\r\n')
  child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(registeredAck())}\r\n`)

  assert.deepEqual(await ready, { port: 54_321, desktopScopeProtocol: 2 })
  assert.deepEqual(await ack, registeredAck())
  assert.deepEqual(logs, ['ordinary log'])
  channel.close()
})

test('ignores duplicate, malformed, or unmatched ACKs and times out a specific waiter', async () => {
  vi.useFakeTimers()
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })

  const wait = channel.expectAck(
    value => value.operation === 'scope_token_promoted' && value.transition_id === TRANSITION_ID,
    500
  )

  child.stdout.write('ANSATZ_SCOPE_CONTROL_V2 not-json\n')
  child.stdout.write('ANSATZ_SCOPE_CONTROL_V2\t{"bearer":"must-not-log"}\n')
  child.stdout.write('ANSATZ_SCOPE_CONTROL_V2{"bearer":"must-not-log"}\n')
  child.stdout.write('ANSATZ_SCOPE_CONTROL_V2 []\n')
  child.stdout.write(
    'ANSATZ_SCOPE_CONTROL_V2 {"__proto__":{"polluted":true},"bearer":"must-not-log"}\n'
  )
  child.stdout.write(
    `ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify({ ...promotedAck(), bearer: 'must-not-log' })}\n`
  )
  child.stdout.write(
    `ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(promotedAck('VVVVVVVVVVVVVVVVVVVVVQ'))}\n`
  )
  child.stdout.write('normal log\n')
  const rejected = assert.rejects(wait, /scope control ACK timeout/)
  await vi.advanceTimersByTimeAsync(501)

  await rejected
  assert.deepEqual(logs, ['normal log'])
  channel.close()
})

test('swallows oversized control lines without poisoning later ACKs', async () => {
  const child = fakeChild()
  const logs: string[] = []
  const channel = new BackendControlChannel(child, { onLog: line => logs.push(line) })
  const ack = channel.expectAck(value => value.operation === 'scope_token_registered', 1_000)

  child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${'x'.repeat(4_096)}\n`)
  child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(registeredAck())}\n`)

  assert.deepEqual(await ack, registeredAck())
  assert.deepEqual(logs, [])
  channel.close()
})

test('closes the control channel when an unterminated stdout line exceeds its bound', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const ack = channel.expectAck(() => true, 1_000)

  child.stdout.write('x'.repeat(1_048_577))

  await assert.rejects(ack, /stdout line exceeded the size limit/i)
  await assert.rejects(
    channel.waitForReady({ timeoutMs: 1_000 }),
    /stdout line exceeded the size limit/i
  )
})

test('reads protocol v2 readiness from the ready-file side channel', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'backend-control-ready-'))
  const readyFile = path.join(directory, 'ready.json')

  try {
    assert.equal(child.stdout.listenerCount('data'), 1)
    const ready = channel.waitForReady({ readyFile, timeoutMs: 1_000 })
    assert.equal(child.stdout.listenerCount('data'), 1)
    setTimeout(() => {
      fs.writeFileSync(readyFile, JSON.stringify({ port: 43_210, desktop_scope_protocol: 2 }))
    }, 20)
    assert.deepEqual(await ready, { port: 43_210, desktopScopeProtocol: 2 })
  } finally {
    channel.close()
    assert.equal(child.stdout.listenerCount('data'), 0)
    fs.rmSync(directory, { force: true, recursive: true })
  }
})

test('registers the ACK waiter before writing a request frame', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const frame = '{"version":2,"operation":"register_scope_token"}\n'

  child.stdin.once('data', chunk => {
    assert.equal(chunk.toString(), frame)
    child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(registeredAck())}\n`)
  })

  const ack = await channel.request(
    frame,
    value => value.operation === 'scope_token_registered' && value.registration_id === REGISTRATION_ID,
    1_000
  )

  assert.deepEqual(ack, registeredAck())
  channel.close()
})

test('rejects every pending ready and ACK waiter when the child exits', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const ready = channel.waitForReady({ timeoutMs: 1_000 })
  const ack = channel.expectAck(() => true, 1_000)

  child.emit('exit', 17, null)

  await assert.rejects(ready, /backend control channel closed.*17/i)
  await assert.rejects(ack, /backend control channel closed.*17/i)
})

test('rejects the request and closes the channel when stdin is not writable', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  child.stdin.destroy()

  await assert.rejects(
    channel.request('{}\n', () => true, 1_000),
    /backend control stdin is not writable/i
  )
  await assert.rejects(channel.expectAck(() => true, 1_000), /backend control stdin is not writable/i)
})

test('rejects all waiters when the stdin write callback reports failure', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const other = channel.expectAck(() => false, 1_000)
  child.stdin.write = vi.fn((_frame, callback) => {
    queueMicrotask(() => callback(new Error('EPIPE')))

    return true
  }) as typeof child.stdin.write

  const request = channel.request('{}\n', () => true, 1_000)

  await assert.rejects(request, /EPIPE/)
  await assert.rejects(other, /EPIPE/)
  await assert.rejects(channel.waitForReady({ timeoutMs: 1_000 }), /EPIPE/)
})

test('rejects all waiters when stdin.write throws synchronously', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  const other = channel.expectAck(() => false, 1_000)
  child.stdin.write = vi.fn(() => {
    throw new Error('sync EPIPE')
  }) as typeof child.stdin.write

  const request = channel.request('{}\n', () => true, 1_000)

  await assert.rejects(request, /sync EPIPE/)
  await assert.rejects(other, /sync EPIPE/)
  await assert.rejects(channel.waitForReady({ timeoutMs: 1_000 }), /sync EPIPE/)
})

test('one ACK resolves only the first matching waiter', async () => {
  const child = fakeChild()
  const channel = new BackendControlChannel(child, { onLog: () => undefined })
  let secondSettled = false
  const first = channel.expectAck(() => true, 1_000)

  const second = channel.expectAck(() => true, 1_000).then(value => {
    secondSettled = true

    return value
  })

  child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(registeredAck())}\n`)
  assert.deepEqual(await first, registeredAck())
  await Promise.resolve()
  assert.equal(secondSettled, false)

  child.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(promotedAck())}\n`)
  assert.deepEqual(await second, promotedAck())
  channel.close()
})
