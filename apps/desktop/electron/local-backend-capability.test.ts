import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { test } from 'vitest'

import type { ConnectionScope } from './auth-bridge'
import type { ScopeControlAck } from './auth-scope-token'
import { LocalBackendCapabilityLifecycle, LocalRuntimeProtocolError } from './local-backend-capability'
import {
  LocalBackendCapabilityUnavailableError,
  LocalCapabilityManager,
  type LocalCapabilityProbe
} from './local-capability-manager'

const SCOPE: ConnectionScope = {
  connection_id: 'local',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  epoch: 11
}

type ControlFrame = Record<string, unknown> & { operation: string }

class FakeChild extends EventEmitter {
  readonly stdin = new PassThrough()
  readonly stdout = new PassThrough()
  readonly frames: ControlFrame[] = []
  private inputBuffer = ''

  constructor() {
    super()
    this.stdin.on('data', chunk => {
      this.inputBuffer += chunk.toString()
      let newline = this.inputBuffer.indexOf('\n')

      while (newline !== -1) {
        this.frames.push(JSON.parse(this.inputBuffer.slice(0, newline)) as ControlFrame)
        this.inputBuffer = this.inputBuffer.slice(newline + 1)
        newline = this.inputBuffer.indexOf('\n')
      }
    })
  }

  async nextFrame(operation: string): Promise<ControlFrame> {
    for (let attempt = 0; attempt < 1_000; attempt += 1) {
      const frame = this.frames.find(candidate => candidate.operation === operation)

      if (frame) {
        return frame
      }

      await Promise.resolve()
    }

    throw new Error(`Timed out waiting for ${operation}`)
  }

  emitAck(ack: ScopeControlAck): void {
    this.stdout.write(`ANSATZ_SCOPE_CONTROL_V2 ${JSON.stringify(ack)}\n`)
  }
}

function registeredAck(frame: ControlFrame): ScopeControlAck {
  return {
    version: 2,
    operation: 'scope_token_registered',
    registration_id: String(frame.registration_id),
    connection_id: String(frame.connection_id),
    runtime_instance_id: String(frame.runtime_instance_id),
    epoch: Number(frame.epoch),
    ttl_seconds: Number(frame.ttl_seconds)
  }
}

function promotedAck(frame: ControlFrame): ScopeControlAck {
  return {
    version: 2,
    operation: 'scope_token_promoted',
    transition_id: String(frame.transition_id),
    registration_id: String(frame.registration_id),
    previous_registration_id: frame.previous_registration_id === null ? null : String(frame.previous_registration_id),
    connection_id: String(frame.connection_id),
    runtime_instance_id: String(frame.runtime_instance_id),
    epoch: Number(frame.epoch),
    overlap_seconds: Number(frame.overlap_seconds)
  }
}

function capabilityFixture() {
  let candidate: ControlFrame | null = null
  const clock = { now: 100 }
  const stoppedChildren: FakeChild[] = []

  const manager = new LocalCapabilityManager({
    clock: () => clock.now,
    probe: async (_baseUrl, _bearer, signal): Promise<LocalCapabilityProbe> => {
      assert.equal(signal.aborted, false)
      assert.ok(candidate)

      return {
        protocol_version: 2,
        registration_id: String(candidate.registration_id),
        connection_id: String(candidate.connection_id),
        runtime_instance_id: String(candidate.runtime_instance_id),
        epoch: Number(candidate.epoch),
        state: 'candidate',
        promoted_transition_id: null
      }
    }
  })

  const lifecycle = new LocalBackendCapabilityLifecycle(manager, {
    onControlClosed: child => stoppedChildren.push(child as FakeChild)
  })

  const start = (key: string, child: FakeChild) =>
    lifecycle.prepare({ child, key, onLog: () => undefined, scope: SCOPE })

  const acknowledgeRegistration = async (child: FakeChild, beforeAck = () => undefined) => {
    candidate = await child.nextFrame('register_scope_token')
    beforeAck()
    child.emitAck(registeredAck(candidate))
  }

  const acknowledgePromotion = async (child: FakeChild) => {
    const promotion = await child.nextFrame('promote_scope_token')
    child.emitAck(promotedAck(promotion))
  }

  const complete = async (preparing: ReturnType<typeof start>, child: FakeChild) => {
    child.stdout.write('HERMES_BACKEND_READY port=43210 desktop_scope_protocol=2\n')
    await acknowledgeRegistration(child)
    await acknowledgePromotion(child)

    return preparing
  }

  const prepare = (key: string, child: FakeChild) => complete(start(key, child), child)

  return {
    acknowledgePromotion,
    acknowledgeRegistration,
    clock,
    complete,
    lifecycle,
    manager,
    prepare,
    start,
    stoppedChildren
  }
}

test('prepares protocol-v2 backends through the control channel with monotonic generations', async () => {
  const fixture = capabilityFixture()
  const firstChild = new FakeChild()
  const secondChild = new FakeChild()

  const first = await fixture.prepare('pool:first', firstChild)
  const second = await fixture.prepare('pool:second', secondChild)

  assert.equal(first.baseUrl, 'http://127.0.0.1:43210')
  assert.equal(first.snapshot.backendGeneration, 1)
  assert.equal(second.snapshot.backendGeneration, 2)
  assert.equal(firstChild.frames[0]?.operation, 'register_scope_token')
  assert.equal(firstChild.frames[1]?.operation, 'promote_scope_token')

  fixture.lifecycle.revoke('pool:first')
  fixture.lifecycle.revoke('pool:second')
})

test('rejects a backend that does not explicitly announce desktop scope protocol v2', async () => {
  for (const readyLine of [
    'HERMES_BACKEND_READY port=43210',
    'HERMES_BACKEND_READY port=43210 desktop_scope_protocol=1'
  ]) {
    const fixture = capabilityFixture()
    const child = new FakeChild()

    const preparing = fixture.lifecycle.prepare({
      child,
      key: 'primary',
      onLog: () => undefined,
      scope: SCOPE
    })

    child.stdout.write(`${readyLine}\n`)

    await assert.rejects(
      preparing,
      error =>
        error instanceof LocalRuntimeProtocolError &&
        error.code === 'local_runtime_protocol_mismatch' &&
        !/auth|login/i.test(error.message)
    )
    assert.equal(child.frames.length, 0)
    assert.equal(child.stdin.writableEnded, true)
  }
})

test('child exit revokes its exact capability and closes the control input with EOF', async () => {
  const fixture = capabilityFixture()
  const child = new FakeChild()
  await fixture.prepare('primary', child)

  assert.equal(fixture.lifecycle.snapshot('primary').backendGeneration, 1)
  child.emit('exit', 0, null)

  assert.throws(() => fixture.lifecycle.snapshot('primary'), LocalBackendCapabilityUnavailableError)
  assert.equal(child.stdin.writableEnded, true)
  assert.equal(fixture.stoppedChildren.includes(child), true)
})

test('descriptor snapshots adopt the background manager token without foreground refresh', async () => {
  const fixture = capabilityFixture()
  const child = new FakeChild()
  const prepared = await fixture.prepare('primary', child)

  const descriptor = {
    authMode: 'scope',
    localCapabilityKey: 'primary',
    token: 'stale'
  }

  fixture.lifecycle.snapshotDescriptor(descriptor)

  assert.equal(descriptor.token, prepared.snapshot.bearer)
  fixture.lifecycle.revokeByChild(child)
  assert.equal(child.stdin.writableEnded, true)
})

test('active capability expiry closes the backend control input with EOF', async () => {
  const fixture = capabilityFixture()
  const child = new FakeChild()
  const prepared = await fixture.prepare('primary', child)

  fixture.clock.now = prepared.snapshot.validUntil

  assert.throws(() => fixture.lifecycle.snapshot('primary'), LocalBackendCapabilityUnavailableError)
  assert.equal(child.stdin.writableEnded, true)
  assert.equal(fixture.stoppedChildren.includes(child), true)
})

test('a stale generation cannot register after a newer preparation already owns the key', async () => {
  const fixture = capabilityFixture()
  const staleChild = new FakeChild()
  const currentChild = new FakeChild()
  const stalePreparation = fixture.start('primary', staleChild)
  const currentPreparation = fixture.start('primary', currentChild)
  let staleOutcome: 'pending' | 'rejected' = 'pending'

  void stalePreparation.catch(() => {
    staleOutcome = 'rejected'
  })

  const current = await fixture.complete(currentPreparation, currentChild)
  assert.equal(current.snapshot.backendGeneration, 2)

  staleChild.stdout.write('HERMES_BACKEND_READY port=43211 desktop_scope_protocol=2\n')

  try {
    for (let attempt = 0; attempt < 1_000; attempt += 1) {
      if (staleOutcome !== 'pending' || staleChild.frames.length > 0) {
        break
      }

      await Promise.resolve()
    }

    assert.equal(staleChild.frames.length, 0)
    await assert.rejects(stalePreparation, LocalBackendCapabilityUnavailableError)
    assert.equal(fixture.lifecycle.snapshot('primary').backendGeneration, 2)
  } finally {
    fixture.lifecycle.revokeByChild(staleChild)
    fixture.lifecycle.revokeByChild(currentChild)
    await stalePreparation.catch(() => undefined)
  }
})

test('an old child exit during replacement cannot revoke the new control handshake', async () => {
  const fixture = capabilityFixture()
  const oldChild = new FakeChild()
  const newChild = new FakeChild()
  await fixture.prepare('primary', oldChild)

  const replacing = fixture.start('primary', newChild)
  newChild.stdout.write('HERMES_BACKEND_READY port=43210 desktop_scope_protocol=2\n')
  await fixture.acknowledgeRegistration(newChild, () => oldChild.emit('exit', 0, null))
  await fixture.acknowledgePromotion(newChild)

  const replacement = await replacing
  assert.equal(replacement.snapshot.backendGeneration, 2)
  assert.equal(fixture.lifecycle.snapshot('primary').backendGeneration, 2)
  fixture.lifecycle.revokeByChild(newChild)
})

test('a superseded in-flight activation cleans up only its own control', async () => {
  const fixture = capabilityFixture()
  const staleChild = new FakeChild()
  const currentChild = new FakeChild()
  const stalePreparation = fixture.start('primary', staleChild)

  const staleRejected = assert.rejects(stalePreparation, LocalBackendCapabilityUnavailableError)

  staleChild.stdout.write('HERMES_BACKEND_READY port=43210 desktop_scope_protocol=2\n')
  await staleChild.nextFrame('register_scope_token')

  const currentPreparation = fixture.start('primary', currentChild)
  const current = await fixture.complete(currentPreparation, currentChild)

  await staleRejected
  assert.equal(current.snapshot.backendGeneration, 2)
  assert.equal(fixture.lifecycle.snapshot('primary').backendGeneration, 2)
  fixture.lifecycle.revokeByChild(currentChild)
})
