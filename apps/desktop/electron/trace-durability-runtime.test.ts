import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { ConnectionScope } from './auth-bridge'
import {
  TraceDurabilityCoordinator,
  type TraceDurabilityDiagnostic,
  TraceDurabilityRuntime,
  type TraceDurabilitySession
} from './trace-durability-runtime'
import type { TraceOwner } from './trace-outbox-types'

const ingress = {
  endpoint: 'http://127.0.0.1:49152/v1/traces',
  localBearer: 'a'.repeat(43)
}

function owner(overrides: Partial<TraceOwner> = {}): TraceOwner {
  const accountId = overrides.accountId ?? '11111111-1111-4111-8111-111111111111'

  return {
    accountId,
    accountKey: `account-${accountId}`,
    installationId: '22222222-2222-4222-8222-222222222222',
    sessionId: '33333333-3333-4333-8333-333333333333',
    ...overrides
  }
}

function scope(overrides: Partial<ConnectionScope> = {}): ConnectionScope {
  return {
    connection_id: 'local',
    epoch: 7,
    runtime_instance_id: 'runtime-a',
    ...overrides
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(currentResolve => {
    resolve = currentResolve
  })

  return { promise, resolve }
}

function sessionFixture(initialOwner: TraceOwner, initialScope: ConnectionScope) {
  let currentOwner = { ...initialOwner }
  let currentScope = { ...initialScope }
  const compactCalls: number[] = []
  const reboundOwners: TraceOwner[] = []
  const reboundScopes: ConnectionScope[] = []
  const stopCalls: number[] = []
  const triggers: string[] = []
  let stopGate: Promise<void> | null = null

  const session: TraceDurabilitySession = {
    async compactIfIdle() {
      compactCalls.push(compactCalls.length + 1)

      return true
    },
    context: () => ({ ingress, owner: currentOwner, scope: currentScope }),
    rebind(nextOwner, nextScope) {
      currentOwner = { ...nextOwner }
      currentScope = { ...nextScope }
      reboundOwners.push({ ...nextOwner })
      reboundScopes.push({ ...nextScope })
    },
    async stop(flushMs) {
      stopCalls.push(flushMs)
      await stopGate
    },
    trigger(reason) {
      triggers.push(reason)
    }
  }

  return {
    compactCalls,
    reboundOwners,
    reboundScopes,
    session,
    setStopGate: (gate: Promise<void>) => {
      stopGate = gate
    },
    stopCalls,
    triggers
  }
}

test('same-account owner activation rebinds one durable session without replacing ingress', async () => {
  const ownerA = owner()
  const ownerB = owner({ sessionId: '44444444-4444-4444-8444-444444444444' })
  const activeScope = scope()
  const fixture = sessionFixture(ownerA, activeScope)
  const diagnostics: TraceDurabilityDiagnostic[] = []
  const runtime = new TraceDurabilityRuntime(event => diagnostics.push(event))
  let createCalls = 0

  const createSession = async () => {
    createCalls += 1

    return fixture.session
  }

  const first = await runtime.activate({ owner: ownerA, scope: activeScope }, createSession)
  const second = await runtime.activate({ owner: ownerB, scope: activeScope }, createSession)

  assert.equal(first.kind, 'created')
  assert.equal(second.kind, 'rebound')
  assert.equal(createCalls, 1)
  assert.deepEqual(fixture.stopCalls, [])
  assert.deepEqual(fixture.reboundOwners, [ownerB])
  assert.deepEqual(runtime.current()?.ingress, first.context.ingress)
  assert.deepEqual(diagnostics, [{ code: 'trace_admission_ready' }, { code: 'trace_owner_rebound' }])
})

test('coordinator installs one facade transport across same-account rebind and detaches only on hard lock', async () => {
  const ownerA = owner()
  const ownerB = owner({ sessionId: '44444444-4444-4444-8444-444444444444' })
  const activeScope = scope()
  const fixture = sessionFixture(ownerA, activeScope)

  const stableDescriptor = {
    endpoint: 'http://127.0.0.1:4318/v1/traces',
    localBearer: 'stable-facade-bearer'
  }

  const facadeCalls: string[] = []
  const backendTransportDescriptors: (typeof stableDescriptor)[] = []
  const runtime = new TraceDurabilityRuntime()

  const coordinator = new TraceDurabilityCoordinator({
    createSession: async () => fixture.session,
    facade: {
      detach: () => facadeCalls.push('detach'),
      install: () => facadeCalls.push('install'),
      rotateBearer: () => facadeCalls.push('rotateBearer')
    },
    onAdmissionReady: () => {
      backendTransportDescriptors.push({ ...stableDescriptor })
    },
    runtime
  })

  await coordinator.activate({ owner: ownerA, scope: activeScope })
  assert.deepEqual(facadeCalls, ['install'])
  assert.deepEqual(backendTransportDescriptors, [stableDescriptor])

  await coordinator.applySameAccountOwner(ownerB)
  assert.deepEqual(facadeCalls, ['install'])
  assert.deepEqual(backendTransportDescriptors, [stableDescriptor])
  assert.deepEqual(runtime.current()?.owner, ownerB)

  await coordinator.lock('signed_out')
  assert.deepEqual(facadeCalls, ['install', 'detach', 'rotateBearer'])
  assert.equal(runtime.current(), null)
})

test('coordinator fences an activation whose facade publication overlaps a hard lock', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const admissionReady = deferred<void>()
  const facadeInstalled = deferred<void>()
  const facadeCalls: string[] = []
  const runtime = new TraceDurabilityRuntime()

  const coordinator = new TraceDurabilityCoordinator({
    createSession: async () => fixture.session,
    facade: {
      detach: () => facadeCalls.push('detach'),
      install: () => {
        facadeCalls.push('install')
        facadeInstalled.resolve()
      },
      rotateBearer: () => facadeCalls.push('rotateBearer')
    },
    onAdmissionReady: () => admissionReady.promise,
    runtime
  })

  const activation = coordinator.activate({ owner: activeOwner, scope: activeScope })

  await facadeInstalled.promise
  assert.deepEqual(facadeCalls, ['install'])
  const locking = coordinator.lock('signed_out')

  admissionReady.resolve()
  await assert.rejects(activation, /trace_activation_superseded/)
  await locking
  assert.deepEqual(facadeCalls, ['install', 'detach', 'rotateBearer'])
  assert.equal(runtime.current(), null)
  assert.deepEqual(fixture.stopCalls, [3_000])
})

test('concurrent coordinator activations wait for the first facade transport publication', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const admissionReady = deferred<void>()
  const facadeInstalled = deferred<void>()
  const facadeCalls: string[] = []
  const runtime = new TraceDurabilityRuntime()

  const coordinator = new TraceDurabilityCoordinator({
    createSession: async () => fixture.session,
    facade: {
      detach: () => facadeCalls.push('detach'),
      install: () => {
        facadeCalls.push('install')
        facadeInstalled.resolve()
      },
      rotateBearer: () => facadeCalls.push('rotateBearer')
    },
    onAdmissionReady: () => admissionReady.promise,
    runtime
  })

  let joinedResolved = false

  const creator = coordinator.activate({ owner: activeOwner, scope: activeScope })
  await facadeInstalled.promise

  const joined = coordinator.activate({ owner: activeOwner, scope: activeScope }).then(result => {
    joinedResolved = true

    return result
  })

  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(joinedResolved, false)

  admissionReady.resolve()
  const [created, reused] = await Promise.all([creator, joined])
  assert.equal(created.kind, 'created')
  assert.equal(reused.kind, 'reused')
  assert.deepEqual(facadeCalls, ['install'])
})

test('one hundred concurrent same-account activations create exactly one session', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const created = deferred<TraceDurabilitySession>()
  const runtime = new TraceDurabilityRuntime()
  let createCalls = 0

  const createSession = () => {
    createCalls += 1

    return created.promise
  }

  const activations = Array.from({ length: 100 }, () =>
    runtime.activate({ owner: activeOwner, scope: activeScope }, createSession)
  )

  assert.equal(createCalls, 1)
  created.resolve(fixture.session)
  const results = await Promise.all(activations)

  assert.equal(results.filter(result => result.kind === 'created').length, 1)
  assert.equal(results.filter(result => result.kind === 'reused').length, 99)
  assert.equal(createCalls, 1)
})

test('an account switch requires a completed lock before another session can activate', async () => {
  const ownerA = owner()
  const ownerB = owner({ accountId: '55555555-5555-4555-8555-555555555555' })
  const activeScope = scope()
  const fixture = sessionFixture(ownerA, activeScope)
  const runtime = new TraceDurabilityRuntime()

  await runtime.activate({ owner: ownerA, scope: activeScope }, async () => fixture.session)

  await assert.rejects(
    runtime.activate({ owner: ownerB, scope: activeScope }, async () => fixture.session),
    /trace_account_switch_requires_lock/
  )
  assert.deepEqual(fixture.reboundOwners, [])
  assert.deepEqual(runtime.current()?.owner, ownerA)
})

test('lock clears the published context and waits for the active session stop', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const stopped = deferred<void>()
  const diagnostics: TraceDurabilityDiagnostic[] = []
  const runtime = new TraceDurabilityRuntime(event => diagnostics.push(event))

  fixture.setStopGate(stopped.promise)
  await runtime.activate({ owner: activeOwner, scope: activeScope }, async () => fixture.session)

  const locking = runtime.lock(2_500)

  assert.equal(runtime.current(), null)
  await Promise.resolve()
  assert.deepEqual(fixture.stopCalls, [2_500])

  stopped.resolve()
  await locking
  assert.deepEqual(diagnostics.at(-1), { code: 'trace_terminal_locked' })
})

test('lock supersedes an in-flight create, waits for its stop, and never publishes it', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const created = deferred<TraceDurabilitySession>()
  const stopped = deferred<void>()
  const runtime = new TraceDurabilityRuntime()

  fixture.setStopGate(stopped.promise)
  const activation = runtime.activate({ owner: activeOwner, scope: activeScope }, () => created.promise)
  const locking = runtime.lock()

  created.resolve(fixture.session)
  await Promise.resolve()
  assert.deepEqual(fixture.stopCalls, [0])
  assert.equal(runtime.current(), null)

  stopped.resolve()
  await assert.rejects(activation, /trace_activation_superseded/)
  await locking
  assert.equal(runtime.current(), null)
})

test('an activation arriving during lock is superseded without resurrecting the locked account', async () => {
  const ownerA = owner()
  const ownerB = owner({ accountId: '77777777-7777-4777-8777-777777777777' })
  const activeScope = scope()
  const fixtureA = sessionFixture(ownerA, activeScope)
  const fixtureB = sessionFixture(ownerB, activeScope)
  const stopped = deferred<void>()
  const runtime = new TraceDurabilityRuntime()
  let staleCreateCalls = 0

  await runtime.activate({ owner: ownerA, scope: activeScope }, async () => fixtureA.session)
  fixtureA.setStopGate(stopped.promise)
  const locking = runtime.lock()

  const staleActivation = runtime.activate({ owner: ownerA, scope: activeScope }, async () => {
    staleCreateCalls += 1

    return fixtureA.session
  })

  stopped.resolve()
  await locking
  await assert.rejects(staleActivation, /trace_activation_superseded/)
  assert.equal(staleCreateCalls, 0)
  assert.equal(runtime.current(), null)

  const replacement = await runtime.activate({ owner: ownerB, scope: activeScope }, async () => fixtureB.session)
  assert.equal(replacement.kind, 'created')
  assert.deepEqual(runtime.current()?.owner, ownerB)
})

test('lock between create publication and activation return rejects instead of returning a null context', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const runtime = new TraceDurabilityRuntime()

  const activation = runtime.activate({ owner: activeOwner, scope: activeScope }, async () => fixture.session)
  let locking: Promise<void> | null = null

  queueMicrotask(() => {
    locking = runtime.lock(1_234)
  })

  await assert.rejects(activation, /trace_activation_superseded/)
  assert.ok(locking)
  await locking
  assert.deepEqual(fixture.stopCalls, [1_234])
  assert.equal(runtime.current(), null)
})

test('runtime forwards recovery and compaction while returning defensive context snapshots', async () => {
  const activeOwner = owner()
  const activeScope = scope()
  const fixture = sessionFixture(activeOwner, activeScope)
  const runtime = new TraceDurabilityRuntime()

  await runtime.activate({ owner: activeOwner, scope: activeScope }, async () => fixture.session)
  runtime.trigger('resume')
  assert.equal(await runtime.compactIfIdle(), true)

  const snapshot = runtime.current()
  assert.ok(snapshot)
  snapshot.ingress.endpoint = 'http://127.0.0.1:1/v1/traces'
  snapshot.owner.sessionId = '66666666-6666-4666-8666-666666666666'
  snapshot.scope.epoch = 99

  assert.deepEqual(fixture.triggers, ['resume'])
  assert.deepEqual(fixture.compactCalls, [1])
  assert.deepEqual(runtime.current(), { ingress, owner: activeOwner, scope: activeScope })
})
