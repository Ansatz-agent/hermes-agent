import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'

import { afterEach, test, vi } from 'vitest'

import {
  AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS,
  AUTH_SCOPE_TOKEN_TTL_SECONDS,
  type AuthScopeToken,
  type ScopeControlAck
} from './auth-scope-token'
import type { BackendControlChannel } from './backend-control-channel'
import {
  LocalBackendCapabilityUnavailableError,
  type LocalCapabilityBinding,
  LocalCapabilityManager,
  type LocalCapabilityManagerOptions
} from './local-capability-manager'

type ControlFrame = Record<string, unknown> & { operation: string }

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (error: Error) => void
}

type PendingControl = {
  frame: ControlFrame
  match: (value: ScopeControlAck) => boolean
  deferred: Deferred<ScopeControlAck>
}

type PendingProbe = {
  baseUrl: string
  bearer: string
  deferred: Deferred<ProbeResult> | null
}

type ProbeResult = {
  protocol_version: 2
  registration_id: string
  connection_id: string
  runtime_instance_id: string
  epoch: number
  state: 'candidate' | 'active' | 'overlap'
  promoted_transition_id: string | null
}

const SCOPE = {
  connection_id: 'local',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  epoch: 7
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, resolve, reject }
}

async function eventually<T>(read: () => T | null, message: string): Promise<T> {
  for (let attempt = 0; attempt < 1_000; attempt += 1) {
    const value = read()

    if (value !== null) {
      return value
    }

    await Promise.resolve()
  }

  throw new Error(message)
}

class FakeControl {
  readonly requests: ControlFrame[] = []
  readonly pending: PendingControl[] = []
  closedReason: Error | null = null

  request(
    encoded: string,
    match: (value: ScopeControlAck) => boolean,
    _timeoutMs?: number
  ): Promise<ScopeControlAck> {
    if (this.closedReason) {
      return Promise.reject(this.closedReason)
    }

    const frame = JSON.parse(encoded) as ControlFrame
    const response = deferred<ScopeControlAck>()
    this.requests.push(frame)
    this.pending.push({ frame, match, deferred: response })

    return response.promise
  }

  close(reason = new Error('fake control closed')): void {
    if (this.closedReason) {
      return
    }

    this.closedReason = reason

    for (const request of this.pending.splice(0)) {
      request.deferred.reject(reason)
    }
  }

  operationCount(operation: string): number {
    return this.requests.filter(frame => frame.operation === operation).length
  }

  async waitForPending(operation: string): Promise<ControlFrame> {
    return eventually(
      () => this.pending.find(request => request.frame.operation === operation)?.frame ?? null,
      `Timed out waiting for ${operation}`
    )
  }

  pendingFrame(operation: string): ControlFrame | null {
    return this.pending.find(request => request.frame.operation === operation)?.frame ?? null
  }

  emit(ack: ScopeControlAck): boolean {
    const index = this.pending.findIndex(request => request.match(ack))

    if (index === -1) {
      return false
    }

    const [request] = this.pending.splice(index, 1)
    request.deferred.resolve(ack)

    return true
  }

  ackRegistered(overrides: Partial<ScopeControlAck> = {}): boolean {
    const frame = this.pendingFrame('register_scope_token')

    if (!frame) {
      return false
    }

    return this.emit({
      version: 2,
      operation: 'scope_token_registered',
      registration_id: String(frame.registration_id),
      connection_id: String(frame.connection_id),
      runtime_instance_id: String(frame.runtime_instance_id),
      epoch: Number(frame.epoch),
      ttl_seconds: Number(frame.ttl_seconds),
      ...overrides
    } as ScopeControlAck)
  }

  ackPromoted(overrides: Partial<ScopeControlAck> = {}): boolean {
    const frame = this.pendingFrame('promote_scope_token')

    if (!frame) {
      return false
    }

    return this.emit({
      version: 2,
      operation: 'scope_token_promoted',
      transition_id: String(frame.transition_id),
      registration_id: String(frame.registration_id),
      previous_registration_id:
        frame.previous_registration_id === null ? null : String(frame.previous_registration_id),
      connection_id: String(frame.connection_id),
      runtime_instance_id: String(frame.runtime_instance_id),
      epoch: Number(frame.epoch),
      overlap_seconds: Number(frame.overlap_seconds),
      ...overrides
    } as ScopeControlAck)
  }

  rejectPending(operation: string, error: Error): void {
    const index = this.pending.findIndex(request => request.frame.operation === operation)
    assert.notEqual(index, -1)
    const [request] = this.pending.splice(index, 1)
    request.deferred.reject(error)
  }
}

class FakeProbe {
  readonly calls: PendingProbe[] = []

  readonly request = (baseUrl: string, bearer: string): Promise<ProbeResult> => {
    const response = deferred<ProbeResult>()
    this.calls.push({ baseUrl, bearer, deferred: response })

    return response.promise
  }

  async waitForPending(): Promise<PendingProbe> {
    return eventually(
      () => this.calls.find(call => call.deferred !== null) ?? null,
      'Timed out waiting for capability probe'
    )
  }

  pending(): PendingProbe | null {
    return this.calls.find(call => call.deferred !== null) ?? null
  }

  settlePending(result: ProbeResult | Error): void {
    const index = this.calls.findIndex(call => call.deferred !== null)
    assert.notEqual(index, -1)
    const call = this.calls[index]
    assert.ok(call.deferred)

    if (result instanceof Error) {
      call.deferred.reject(result)
    } else {
      call.deferred.resolve(result)
    }

    // The historical call remains inspectable, but no longer counts as pending.
    call.deferred = null
  }
}

type Fixture = {
  clock: { now: number }
  control: FakeControl
  diagnostics: Array<Record<string, unknown>>
  manager: LocalCapabilityManager
  binding: LocalCapabilityBinding
  probe: FakeProbe
  tokensByBearer: Map<string, AuthScopeToken>
  completePendingRotation: (control?: FakeControl) => Promise<void>
  resolveProbe: (
    state?: ProbeResult['state'],
    promotedTransitionId?: string | null
  ) => void
}

function managerFixture(
  overrides: {
    onDiagnostic?: LocalCapabilityManagerOptions['onDiagnostic']
    random?: () => number
    useDefaultProbe?: boolean
  } = {}
): Fixture {
  vi.useFakeTimers()
  const clock = { now: 100 }
  const control = new FakeControl()
  const probe = new FakeProbe()
  const diagnostics: Array<Record<string, unknown>> = []
  const tokensByBearer = new Map<string, AuthScopeToken>()
  let tokenSequence = 0
  let transitionSequence = 0

  const issueToken = (): AuthScopeToken => {
    tokenSequence += 1
    const bearer = Buffer.alloc(32, tokenSequence).toString('base64url')
    const registrationId = Buffer.alloc(16, tokenSequence + 64).toString('base64url')

    const token: AuthScopeToken = {
      bearer,
      registrationId,
      scope: { ...SCOPE },
      issuedAt: clock.now,
      rotateAt: clock.now + AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS,
      ttlSeconds: AUTH_SCOPE_TOKEN_TTL_SECONDS,
      validUntil: clock.now + AUTH_SCOPE_TOKEN_TTL_SECONDS
    }

    tokensByBearer.set(bearer, token)

    return token
  }

  const options: LocalCapabilityManagerOptions = {
    clock: () => clock.now,
    issueToken,
    issueTransitionId: () => {
      transitionSequence += 1

      return Buffer.alloc(16, transitionSequence + 96).toString('base64url')
    },
    random: overrides.random ?? (() => 0),
    onDiagnostic: overrides.onDiagnostic ?? (event => diagnostics.push(event)),
    ...(overrides.useDefaultProbe ? {} : { probe: probe.request })
  }

  const manager = new LocalCapabilityManager(options)

  const binding: LocalCapabilityBinding = {
    key: 'backend-1',
    baseUrl: 'http://127.0.0.1:43210',
    scope: { ...SCOPE },
    backendGeneration: 1,
    control: control as unknown as BackendControlChannel
  }

  const resolveProbe = (
    state: ProbeResult['state'] = 'candidate',
    promotedTransitionId: string | null = null
  ): void => {
    const call = probe.pending()
    assert.ok(call)
    const token = tokensByBearer.get(call.bearer)
    assert.ok(token)
    probe.settlePending({
      protocol_version: 2,
      registration_id: token.registrationId,
      connection_id: token.scope.connection_id,
      runtime_instance_id: token.scope.runtime_instance_id,
      epoch: token.scope.epoch,
      state,
      promoted_transition_id: promotedTransitionId
    })
  }

  const completePendingRotation = async (targetControl = control): Promise<void> => {
    await targetControl.waitForPending('register_scope_token')
    assert.equal(targetControl.ackRegistered(), true)
    await probe.waitForPending()
    resolveProbe('candidate')
    await targetControl.waitForPending('promote_scope_token')
    assert.equal(targetControl.ackPromoted(), true)
  }

  return {
    clock,
    control,
    diagnostics,
    manager,
    binding,
    probe,
    tokensByBearer,
    completePendingRotation,
    resolveProbe
  }
}

async function activate(fixture: Fixture, binding = fixture.binding): Promise<void> {
  const activating = fixture.manager.activate(binding)
  await fixture.completePendingRotation(binding.control as unknown as FakeControl)
  await activating
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('keeps the old descriptor until candidate ACK, probe, and promotion all finish', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')

  fixture.clock.now = first.rotateAt
  const rotating = fixture.manager.refresh('backend-1', 'timer')
  await fixture.control.waitForPending('register_scope_token')
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  assert.equal(fixture.control.ackRegistered(), true)
  await fixture.probe.waitForPending()
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  fixture.resolveProbe('candidate')
  await fixture.control.waitForPending('promote_scope_token')
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  assert.equal(fixture.control.ackPromoted(), true)
  await rotating
  assert.notEqual(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
})

test('coalesces one hundred concurrent timer and recovery signals', async () => {
  const fixture = managerFixture()
  await activate(fixture)

  const refreshes = Array.from({ length: 100 }, (_, index) =>
    fixture.manager.refresh('backend-1', index % 2 === 0 ? 'timer' : 'recovery')
  )

  await fixture.completePendingRotation()
  await Promise.all(refreshes)
  assert.equal(fixture.control.operationCount('register_scope_token'), 2)
  assert.equal(fixture.control.operationCount('promote_scope_token'), 2)
})

test('retries a register ACK timeout with bounded backoff while the old active stays visible', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  const rotating = fixture.manager.refresh('backend-1', 'timer')

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.rejectPending('register_scope_token', new Error('ACK timeout'))
  await Promise.resolve()
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
  assert.equal(fixture.control.operationCount('register_scope_token'), 2)

  const retryEvent = await eventually(
    () =>
      fixture.diagnostics.find(event => event.name === 'scope_rotation_retry_scheduled') ?? null,
    'Retry diagnostic was not emitted'
  )

  assert.equal(retryEvent.elapsedMs, 0)

  await vi.advanceTimersByTimeAsync(999)
  assert.equal(fixture.control.operationCount('register_scope_token'), 2)
  await vi.advanceTimersByTimeAsync(1)
  await fixture.completePendingRotation()
  await rotating
  assert.equal(fixture.control.operationCount('register_scope_token'), 3)
  assert.notEqual(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
})

test('caps retry jitter at twenty percent', async () => {
  const fixture = managerFixture({ random: () => 99 })
  await activate(fixture)
  const rotating = fixture.manager.refresh('backend-1', 'timer')

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.rejectPending('register_scope_token', new Error('ACK timeout'))
  await eventually(
    () =>
      fixture.diagnostics.find(event => event.name === 'scope_rotation_retry_scheduled') ?? null,
    'Retry diagnostic was not emitted'
  )

  await vi.advanceTimersByTimeAsync(1_199)
  assert.equal(fixture.control.operationCount('register_scope_token'), 2)
  await vi.advanceTimersByTimeAsync(1)
  await fixture.completePendingRotation()
  await rotating
  assert.equal(fixture.control.operationCount('register_scope_token'), 3)
})

test('does not promote after a failed candidate probe and retries without replacing the old active', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  const rotating = fixture.manager.refresh('backend-1', 'recovery')

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.ackRegistered()
  await fixture.probe.waitForPending()
  fixture.probe.settlePending(new Error('probe unavailable'))
  await Promise.resolve()
  assert.equal(fixture.control.operationCount('promote_scope_token'), 1)
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)

  await vi.advanceTimersByTimeAsync(1_000)
  await fixture.completePendingRotation()
  await rotating
  assert.equal(fixture.control.operationCount('promote_scope_token'), 2)
})

test('accepts an identical promoted transition from the probe when the promote ACK is lost', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  const rotating = fixture.manager.refresh('backend-1', 'recovery')

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.ackRegistered()
  await fixture.probe.waitForPending()
  fixture.resolveProbe('candidate')
  const promote = await fixture.control.waitForPending('promote_scope_token')
  fixture.control.rejectPending('promote_scope_token', new Error('promote ACK timeout'))
  await fixture.probe.waitForPending()
  fixture.resolveProbe('active', String(promote.transition_id))

  await rotating
  assert.notEqual(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
})

test('ignores stale, duplicate, wrong-scope, and wrong-transition ACKs until the exact ACK arrives', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  const rotating = fixture.manager.refresh('backend-1', 'timer')
  const registration = await fixture.control.waitForPending('register_scope_token')

  assert.equal(
    fixture.control.emit({
      version: 2,
      operation: 'scope_token_registered',
      registration_id: first.registrationId,
      connection_id: SCOPE.connection_id,
      runtime_instance_id: SCOPE.runtime_instance_id,
      epoch: SCOPE.epoch,
      ttl_seconds: AUTH_SCOPE_TOKEN_TTL_SECONDS
    }),
    false
  )
  assert.equal(
    fixture.control.ackRegistered({ connection_id: 'other-connection' } as Partial<ScopeControlAck>),
    false
  )
  assert.equal(fixture.probe.pending(), null)
  assert.equal(fixture.control.ackRegistered(), true)
  await fixture.probe.waitForPending()
  fixture.resolveProbe('candidate')

  const promotion = await fixture.control.waitForPending('promote_scope_token')
  assert.equal(
    fixture.control.ackPromoted({
      transition_id: Buffer.alloc(16, 0xee).toString('base64url')
    } as Partial<ScopeControlAck>),
    false
  )
  assert.equal(fixture.manager.snapshot('backend-1').registrationId, first.registrationId)
  assert.equal(fixture.control.ackPromoted(), true)
  await rotating

  assert.equal(
    fixture.manager.snapshot('backend-1').registrationId,
    String(registration.registration_id)
  )
  assert.equal(String(promotion.previous_registration_id), first.registrationId)
})

test('revoke cancels an in-flight refresh and late ACKs cannot resurrect the descriptor', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const rotating = fixture.manager.refresh('backend-1', 'recovery')
  await fixture.control.waitForPending('register_scope_token')

  fixture.manager.revoke('backend-1')

  await assert.rejects(
    rotating,
    error =>
      error instanceof LocalBackendCapabilityUnavailableError &&
      error.code === 'local_backend_unavailable'
  )
  assert.equal(fixture.control.closedReason instanceof LocalBackendCapabilityUnavailableError, true)
  assert.equal(fixture.control.ackRegistered(), false)
  assert.throws(
    () => fixture.manager.snapshot('backend-1'),
    error => error instanceof LocalBackendCapabilityUnavailableError
  )
})

test('revokeByControl clears every descriptor owned by the terminated control channel', async () => {
  const fixture = managerFixture()
  await activate(fixture)

  fixture.manager.revokeByControl(fixture.binding.control)

  assert.throws(
    () => fixture.manager.snapshot('backend-1'),
    LocalBackendCapabilityUnavailableError
  )
  assert.equal(fixture.control.closedReason instanceof LocalBackendCapabilityUnavailableError, true)
  assert.equal(
    fixture.diagnostics.find(event => event.name === 'scope_revoked')?.elapsedMs,
    0
  )
})

test('a replacement backend generation cancels the old refresh and owns the only visible descriptor', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const oldRefresh = fixture.manager.refresh('backend-1', 'timer')
  await fixture.control.waitForPending('register_scope_token')

  const replacementControl = new FakeControl()

  const replacementBinding: LocalCapabilityBinding = {
    ...fixture.binding,
    backendGeneration: 2,
    control: replacementControl as unknown as BackendControlChannel
  }

  const replacing = fixture.manager.activate(replacementBinding)

  await assert.rejects(oldRefresh, LocalBackendCapabilityUnavailableError)
  await fixture.completePendingRotation(replacementControl)
  await replacing
  assert.equal(fixture.manager.snapshot('backend-1').backendGeneration, 2)
  assert.equal(fixture.control.ackRegistered(), false)
  assert.equal(fixture.manager.snapshot('backend-1').backendGeneration, 2)
})

test('consecutive failures reaching active expiry surface only a local backend unavailable error', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  fixture.clock.now = first.validUntil - 0.5
  const rotating = fixture.manager.refresh('backend-1', 'timer')

  const rejected = assert.rejects(
    rotating,
    error =>
      error instanceof LocalBackendCapabilityUnavailableError &&
      error.code === 'local_backend_unavailable' &&
      !/auth|login/i.test(error.message)
  )

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.rejectPending('register_scope_token', new Error('ACK timeout'))
  await Promise.resolve()
  fixture.clock.now = first.validUntil
  await vi.advanceTimersByTimeAsync(500)

  await rejected
  assert.throws(
    () => fixture.manager.snapshot('backend-1'),
    LocalBackendCapabilityUnavailableError
  )
})

test('snapshot expiry aborts a sleeping retry and closes its terminal control channel', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  const rotating = fixture.manager.refresh('backend-1', 'timer')
  const rejected = assert.rejects(rotating, LocalBackendCapabilityUnavailableError)

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.rejectPending('register_scope_token', new Error('ACK timeout'))
  await Promise.resolve()
  fixture.clock.now = first.validUntil
  assert.throws(
    () => fixture.manager.snapshot('backend-1'),
    LocalBackendCapabilityUnavailableError
  )

  const closedAtExpiry =
    fixture.control.closedReason instanceof LocalBackendCapabilityUnavailableError

  fixture.manager.revoke('backend-1')
  await rejected
  assert.equal(closedAtExpiry, true)
})

test('a throwing diagnostic observer cannot interrupt activation or rotation', async () => {
  const fixture = managerFixture({
    onDiagnostic: () => {
      throw new Error('diagnostic sink failed')
    }
  })

  await activate(fixture)
  assert.equal(fixture.manager.snapshot('backend-1').backendGeneration, 1)
})

test('the production probe is a non-redirecting GET to the bound loopback backend', async () => {
  const fetchMock = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
    const authorization = new Headers(init?.headers).get('Authorization')
    assert.ok(authorization?.startsWith('Bearer '))
    const token = fixture.tokensByBearer.get(authorization.slice('Bearer '.length))
    assert.ok(token)

    return new Response(
      JSON.stringify({
        protocol_version: 2,
        registration_id: token.registrationId,
        connection_id: token.scope.connection_id,
        runtime_instance_id: token.scope.runtime_instance_id,
        epoch: token.scope.epoch,
        state: 'candidate',
        promoted_transition_id: null
      }),
      { status: 200, headers: { 'content-type': 'application/json' } }
    )
  })

  vi.stubGlobal('fetch', fetchMock)
  const fixture = managerFixture({ useDefaultProbe: true })
  const activating = fixture.manager.activate(fixture.binding)

  await fixture.control.waitForPending('register_scope_token')
  fixture.control.ackRegistered()
  await fixture.control.waitForPending('promote_scope_token')
  fixture.control.ackPromoted()
  await activating

  assert.equal(String(fetchMock.mock.calls[0]?.[0]), `${fixture.binding.baseUrl}/api/auth/scope-token-probe`)
  assert.equal(fetchMock.mock.calls[0]?.[1]?.method, 'GET')
  assert.equal(fetchMock.mock.calls[0]?.[1]?.cache, 'no-store')
  assert.equal(fetchMock.mock.calls[0]?.[1]?.redirect, 'error')
  assert.equal(fetchMock.mock.calls[0]?.[1]?.credentials, 'omit')
})

test('the unref timer starts a background rotation at rotateAt', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const first = fixture.manager.snapshot('backend-1')
  fixture.clock.now = first.rotateAt

  await vi.advanceTimersByTimeAsync(AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS * 1_000)
  await fixture.completePendingRotation()
  await eventually(
    () =>
      fixture.manager.snapshot('backend-1').registrationId !== first.registrationId
        ? fixture.manager.snapshot('backend-1')
        : null,
    'Timer rotation did not replace the active descriptor'
  )
})

test('snapshot returns an immutable copy and diagnostics never contain capability secrets', async () => {
  const fixture = managerFixture()
  await activate(fixture)
  const observed = fixture.manager.snapshot('backend-1')
  observed.scope.connection_id = 'mutated'
  ;(observed as { bearer: string }).bearer = 'mutated'

  const stable = fixture.manager.snapshot('backend-1')
  assert.equal(stable.scope.connection_id, SCOPE.connection_id)
  assert.notEqual(stable.bearer, 'mutated')
  assert.ok(fixture.diagnostics.length > 0)
  assert.deepEqual(
    Object.keys(fixture.diagnostics[0]).sort(),
    ['attempt', 'backendGeneration', 'elapsedMs', 'name']
  )
  const diagnostics = JSON.stringify(fixture.diagnostics)
  assert.equal(diagnostics.includes(stable.bearer), false)
  assert.equal(diagnostics.includes(stable.registrationId), false)
})
