import { uptime } from 'node:os'

import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'
import {
  AUTH_SCOPE_TOKEN_OVERLAP_SECONDS,
  type AuthScopeToken,
  DESKTOP_SCOPE_PROTOCOL_VERSION,
  encodeScopeTokenPromotion,
  encodeScopeTokenRegistration,
  issueAuthScopeToken,
  issueScopeTransitionId,
  type ScopeControlAck
} from './auth-scope-token'
import { type BackendControlChannel } from './backend-control-channel'

const RETRY_DELAYS_SECONDS = [1, 2, 5, 10, 30] as const
const DEFAULT_CONTROL_ACK_TIMEOUT_MS = 5_000
const DEFAULT_CAPABILITY_PROBE_TIMEOUT_MS = 5_000

export type LocalCapabilityBinding = {
  key: string
  baseUrl: string
  scope: ConnectionScope
  backendGeneration: number
  control: BackendControlChannel
}

export type LocalCapabilitySnapshot = {
  key: string
  bearer: string
  registrationId: string
  scope: ConnectionScope
  backendGeneration: number
  issuedAt: number
  rotateAt: number
  validUntil: number
}

export type RotationReason = 'timer' | 'recovery'

export type LocalCapabilityProbe = {
  protocol_version: 2
  registration_id: string
  connection_id: string
  runtime_instance_id: string
  epoch: number
  state: 'candidate' | 'active' | 'overlap'
  promoted_transition_id: string | null
}

export type LocalCapabilityDiagnostic = {
  name: string
  backendGeneration: number
  attempt: number
  elapsedMs: number
}

export type LocalCapabilityManagerOptions = {
  clock?: () => number
  issueToken?: (scope: ConnectionScope) => AuthScopeToken
  issueTransitionId?: () => string
  probe?: (
    baseUrl: string,
    bearer: string,
    signal: AbortSignal
  ) => Promise<LocalCapabilityProbe>
  probeTimeoutMs?: number
  random?: () => number
  onDiagnostic?: (event: LocalCapabilityDiagnostic) => void
}

type CapabilityState = {
  binding: LocalCapabilityBinding
  active: AuthScopeToken | null
  candidate: AuthScopeToken | null
  refreshPromise: Promise<LocalCapabilitySnapshot> | null
  timer: NodeJS.Timeout | null
  retryAttempt: number
  abortController: AbortController
}

export class LocalBackendCapabilityUnavailableError extends Error {
  readonly code = 'local_backend_unavailable'

  constructor(message = 'Local backend capability unavailable', options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'LocalBackendCapabilityUnavailableError'
  }
}

function sameScope(left: ConnectionScope, right: ConnectionScope): boolean {
  return (
    left.connection_id === right.connection_id &&
    left.runtime_instance_id === right.runtime_instance_id &&
    left.epoch === right.epoch
  )
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const observed = Object.keys(value).sort()
  const wanted = [...expected].sort()

  return observed.length === wanted.length && observed.every((key, index) => key === wanted[index])
}

function validatedBinding(binding: LocalCapabilityBinding): LocalCapabilityBinding {
  const scope = requireAuthenticatedConnectionScope(binding.scope)
  let baseUrl: URL

  try {
    baseUrl = new URL(binding.baseUrl)
  } catch {
    throw new TypeError('Invalid local capability binding')
  }

  if (
    typeof binding.key !== 'string' ||
    binding.key.length === 0 ||
    !Number.isSafeInteger(binding.backendGeneration) ||
    binding.backendGeneration <= 0 ||
    baseUrl.protocol !== 'http:' ||
    baseUrl.hostname !== '127.0.0.1' ||
    !baseUrl.port ||
    baseUrl.username ||
    baseUrl.password ||
    baseUrl.search ||
    baseUrl.hash
  ) {
    throw new TypeError('Invalid local capability binding')
  }

  return {
    ...binding,
    baseUrl: baseUrl.origin,
    scope: { ...scope }
  }
}

function validatedProbe(value: unknown): LocalCapabilityProbe {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Invalid local capability probe response')
  }

  const record = value as Record<string, unknown>

  if (
    !exactKeys(record, [
      'protocol_version',
      'registration_id',
      'connection_id',
      'runtime_instance_id',
      'epoch',
      'state',
      'promoted_transition_id'
    ]) ||
    record.protocol_version !== DESKTOP_SCOPE_PROTOCOL_VERSION ||
    typeof record.registration_id !== 'string' ||
    typeof record.connection_id !== 'string' ||
    typeof record.runtime_instance_id !== 'string' ||
    !Number.isSafeInteger(record.epoch) ||
    !['candidate', 'active', 'overlap'].includes(String(record.state)) ||
    !(record.promoted_transition_id === null || typeof record.promoted_transition_id === 'string')
  ) {
    throw new Error('Invalid local capability probe response')
  }

  return record as LocalCapabilityProbe
}

async function defaultProbe(
  baseUrl: string,
  bearer: string,
  signal: AbortSignal
): Promise<LocalCapabilityProbe> {
  const endpoint = new URL('/api/auth/scope-token-probe', baseUrl)

  const response = await fetch(endpoint, {
    method: 'GET',
    headers: { Authorization: `Bearer ${bearer}` },
    cache: 'no-store',
    credentials: 'omit',
    redirect: 'error',
    signal
  })

  if (!response.ok) {
    throw new Error(`Local capability probe failed (${response.status})`)
  }

  return validatedProbe(await response.json())
}

function unavailable(cause?: unknown): LocalBackendCapabilityUnavailableError {
  return new LocalBackendCapabilityUnavailableError('Local backend capability unavailable', {
    cause
  })
}

export class LocalCapabilityManager {
  private readonly states = new Map<string, CapabilityState>()
  private readonly clock: () => number
  private readonly issueToken: (scope: ConnectionScope) => AuthScopeToken
  private readonly issueTransitionId: () => string
  private readonly probe: (
    baseUrl: string,
    bearer: string,
    signal: AbortSignal
  ) => Promise<LocalCapabilityProbe>
  private readonly probeTimeoutMs: number
  private readonly random: () => number
  private readonly onDiagnostic: (event: LocalCapabilityDiagnostic) => void

  constructor(options: LocalCapabilityManagerOptions = {}) {
    const probeTimeoutMs = options.probeTimeoutMs ?? DEFAULT_CAPABILITY_PROBE_TIMEOUT_MS

    if (!Number.isFinite(probeTimeoutMs) || probeTimeoutMs <= 0) {
      throw new TypeError('Invalid local capability probe timeout')
    }

    this.clock = options.clock ?? uptime
    this.issueToken =
      options.issueToken ?? (scope => issueAuthScopeToken(scope, { clock: this.clock }))
    this.issueTransitionId = options.issueTransitionId ?? (() => issueScopeTransitionId())
    this.probe = options.probe ?? defaultProbe
    this.probeTimeoutMs = probeTimeoutMs
    this.random = options.random ?? Math.random
    this.onDiagnostic = options.onDiagnostic ?? (() => undefined)
  }

  activate(binding: LocalCapabilityBinding): Promise<LocalCapabilitySnapshot> {
    let validated: LocalCapabilityBinding

    try {
      validated = validatedBinding(binding)
    } catch (error) {
      return Promise.reject(unavailable(error))
    }

    this.revoke(validated.key)

    const state: CapabilityState = {
      binding: validated,
      active: null,
      candidate: null,
      refreshPromise: null,
      timer: null,
      retryAttempt: 0,
      abortController: new AbortController()
    }

    this.states.set(validated.key, state)

    return this.startRefresh(state, 'recovery')
  }

  snapshot(key: string): LocalCapabilitySnapshot {
    const state = this.states.get(key)

    if (!state || !state.active || this.clock() >= state.active.validUntil) {
      if (state?.active && this.clock() >= state.active.validUntil) {
        this.expire(state)
      }

      throw unavailable()
    }

    return this.toSnapshot(state, state.active)
  }

  refresh(key: string, reason: RotationReason): Promise<LocalCapabilitySnapshot> {
    const state = this.states.get(key)

    if (!state || !state.active || this.clock() >= state.active.validUntil) {
      if (state?.active && this.clock() >= state.active.validUntil) {
        this.expire(state)
      }

      return Promise.reject(unavailable())
    }

    if (state.refreshPromise) {
      return state.refreshPromise
    }

    return this.startRefresh(state, reason)
  }

  revoke(key: string): void {
    const state = this.states.get(key)

    if (!state) {
      return
    }

    const error = unavailable()
    state.abortController.abort(error)
    this.clearTimer(state)
    state.active = null
    state.candidate = null
    this.states.delete(key)
    state.binding.control.close(error)
    this.diagnostic(state, 'scope_revoked', state.retryAttempt, this.clock())
  }

  revokeByControl(control: BackendControlChannel): void {
    for (const [key, state] of [...this.states]) {
      if (state.binding.control === control) {
        this.revoke(key)
      }
    }
  }

  private startRefresh(
    state: CapabilityState,
    reason: RotationReason
  ): Promise<LocalCapabilitySnapshot> {
    if (state.refreshPromise) {
      return state.refreshPromise
    }

    this.clearTimer(state)
    const operation = this.rotateWithRetries(state, reason)

    const tracked = operation.finally(() => {
      if (state.refreshPromise === tracked) {
        state.refreshPromise = null
      }
    })

    state.refreshPromise = tracked

    return tracked
  }

  private async rotateWithRetries(
    state: CapabilityState,
    reason: RotationReason
  ): Promise<LocalCapabilitySnapshot> {
    let attempt = 0
    const refreshStartedAt = this.clock()

    while (this.isCurrent(state)) {
      const active = state.active

      if (active && this.clock() >= active.validUntil) {
        this.expire(state)
        throw unavailable()
      }

      try {
        const snapshot = await this.rotateOnce(state, reason, attempt)
        state.retryAttempt = 0

        if (attempt > 0) {
          this.diagnostic(state, 'scope_rotation_recovered_backend', attempt, refreshStartedAt)
        }

        return snapshot
      } catch (error) {
        if (!this.isCurrent(state) || state.abortController.signal.aborted) {
          throw unavailable(error)
        }

        state.candidate = null

        if (!state.active) {
          throw unavailable(error)
        }

        const now = this.clock()
        const remaining = state.active.validUntil - now

        if (remaining <= 0) {
          this.expire(state)
          throw unavailable(error)
        }

        const baseDelay = RETRY_DELAYS_SECONDS[Math.min(attempt, RETRY_DELAYS_SECONDS.length - 1)]
        const random = Math.min(1, Math.max(0, this.random()))
        const delay = Math.min(baseDelay * (1 + random * 0.2), remaining)
        attempt += 1
        state.retryAttempt = attempt
        this.diagnostic(state, 'scope_rotation_retry_scheduled', attempt, refreshStartedAt)
        await this.waitForRetry(state, delay)
      }
    }

    throw unavailable()
  }

  private async rotateOnce(
    state: CapabilityState,
    _reason: RotationReason,
    attempt: number
  ): Promise<LocalCapabilitySnapshot> {
    this.assertCurrent(state)
    const startedAt = this.clock()
    const candidate = this.issueToken({ ...state.binding.scope })

    if (!sameScope(candidate.scope, state.binding.scope)) {
      throw new Error('Issued local capability has the wrong scope')
    }

    state.candidate = candidate
    this.diagnostic(state, 'scope_rotation_started', attempt, startedAt)

    try {
      await this.awaitCurrent(
        state,
        state.binding.control.request(
          encodeScopeTokenRegistration(candidate),
          ack => this.matchesRegisteredAck(state, candidate, ack),
          DEFAULT_CONTROL_ACK_TIMEOUT_MS
        )
      )
      this.assertCandidate(state, candidate)
      this.diagnostic(state, 'scope_candidate_acknowledged', attempt, startedAt)

      const probe = validatedProbe(
        await this.probeWithTimeout(state, candidate.bearer)
      )

      this.assertCandidateProbe(state, candidate, probe, 'candidate', null)
      this.diagnostic(state, 'scope_candidate_probe_succeeded', attempt, startedAt)

      const transitionId = this.issueTransitionId()
      const previousRegistrationId = state.active?.registrationId ?? null

      try {
        await this.awaitCurrent(
          state,
          state.binding.control.request(
            encodeScopeTokenPromotion(candidate, previousRegistrationId, transitionId),
            ack =>
              this.matchesPromotedAck(
                state,
                candidate,
                previousRegistrationId,
                transitionId,
                ack
              ),
            DEFAULT_CONTROL_ACK_TIMEOUT_MS
          )
        )
      } catch (error) {
        this.assertCandidate(state, candidate)

        const confirmation = validatedProbe(
          await this.probeWithTimeout(state, candidate.bearer)
        )

        this.assertCandidateProbe(state, candidate, confirmation, 'active', transitionId)

        if (state.abortController.signal.aborted) {
          throw unavailable(error)
        }
      }

      this.assertCandidate(state, candidate)
      state.active = candidate
      state.candidate = null
      this.scheduleRotation(state)
      this.diagnostic(state, 'scope_rotation_promoted', attempt, startedAt)

      return this.toSnapshot(state, candidate)
    } catch (error) {
      if (state.candidate === candidate) {
        state.candidate = null
      }

      throw error
    }
  }

  private matchesRegisteredAck(
    state: CapabilityState,
    candidate: AuthScopeToken,
    ack: ScopeControlAck
  ): boolean {
    return (
      this.isCurrent(state) &&
      state.candidate === candidate &&
      ack.operation === 'scope_token_registered' &&
      ack.registration_id === candidate.registrationId &&
      ack.connection_id === state.binding.scope.connection_id &&
      ack.runtime_instance_id === state.binding.scope.runtime_instance_id &&
      ack.epoch === state.binding.scope.epoch &&
      ack.ttl_seconds === candidate.ttlSeconds
    )
  }

  private matchesPromotedAck(
    state: CapabilityState,
    candidate: AuthScopeToken,
    previousRegistrationId: string | null,
    transitionId: string,
    ack: ScopeControlAck
  ): boolean {
    return (
      this.isCurrent(state) &&
      state.candidate === candidate &&
      ack.operation === 'scope_token_promoted' &&
      ack.transition_id === transitionId &&
      ack.registration_id === candidate.registrationId &&
      ack.previous_registration_id === previousRegistrationId &&
      ack.connection_id === state.binding.scope.connection_id &&
      ack.runtime_instance_id === state.binding.scope.runtime_instance_id &&
      ack.epoch === state.binding.scope.epoch &&
      ack.overlap_seconds === AUTH_SCOPE_TOKEN_OVERLAP_SECONDS
    )
  }

  private assertCandidateProbe(
    state: CapabilityState,
    candidate: AuthScopeToken,
    probe: LocalCapabilityProbe,
    expectedState: LocalCapabilityProbe['state'],
    promotedTransitionId: string | null
  ): void {
    this.assertCandidate(state, candidate)

    if (
      probe.protocol_version !== DESKTOP_SCOPE_PROTOCOL_VERSION ||
      probe.registration_id !== candidate.registrationId ||
      probe.connection_id !== state.binding.scope.connection_id ||
      probe.runtime_instance_id !== state.binding.scope.runtime_instance_id ||
      probe.epoch !== state.binding.scope.epoch ||
      probe.state !== expectedState ||
      probe.promoted_transition_id !== promotedTransitionId
    ) {
      throw new Error('Local capability probe did not confirm the pending transition')
    }
  }

  private assertCandidate(state: CapabilityState, candidate: AuthScopeToken): void {
    this.assertCurrent(state)

    if (state.candidate !== candidate) {
      throw unavailable()
    }
  }

  private assertCurrent(state: CapabilityState): void {
    if (!this.isCurrent(state) || state.abortController.signal.aborted) {
      throw unavailable()
    }
  }

  private isCurrent(state: CapabilityState): boolean {
    return this.states.get(state.binding.key) === state
  }

  private awaitCurrent<T>(state: CapabilityState, operation: Promise<T>): Promise<T> {
    if (state.abortController.signal.aborted) {
      return Promise.reject(unavailable(state.abortController.signal.reason))
    }

    return new Promise<T>((resolve, reject) => {
      const onAbort = () => {
        reject(unavailable(state.abortController.signal.reason))
      }

      state.abortController.signal.addEventListener('abort', onAbort, { once: true })
      operation.then(
        value => {
          state.abortController.signal.removeEventListener('abort', onAbort)

          if (this.isCurrent(state)) {
            resolve(value)
          } else {
            reject(unavailable())
          }
        },
        error => {
          state.abortController.signal.removeEventListener('abort', onAbort)
          reject(error)
        }
      )
    })
  }

  private probeWithTimeout(
    state: CapabilityState,
    bearer: string
  ): Promise<LocalCapabilityProbe> {
    return new Promise((resolve, reject) => {
      const controller = new AbortController()
      let timer: NodeJS.Timeout | null = null
      let settled = false

      const cleanup = () => {
        if (timer) {
          clearTimeout(timer)
          timer = null
        }

        state.abortController.signal.removeEventListener('abort', onStateAbort)
        controller.signal.removeEventListener('abort', onProbeAbort)
      }

      const settle = <T>(complete: (value: T) => void, value: T) => {
        if (settled) {
          return
        }

        settled = true
        cleanup()
        complete(value)
      }

      const onProbeAbort = () => {
        settle(reject, controller.signal.reason ?? unavailable())
      }

      const onStateAbort = () => {
        controller.abort(state.abortController.signal.reason)
      }

      controller.signal.addEventListener('abort', onProbeAbort, { once: true })
      state.abortController.signal.addEventListener('abort', onStateAbort, { once: true })

      timer = setTimeout(() => {
        controller.abort(new Error('Local capability probe timed out'))
      }, this.probeTimeoutMs)
      timer.unref?.()

      if (state.abortController.signal.aborted) {
        onStateAbort()

        return
      }

      Promise.resolve()
        .then(() => this.probe(state.binding.baseUrl, bearer, controller.signal))
        .then(
          value => settle(resolve, value),
          error => settle(reject, error)
        )
    })
  }

  private waitForRetry(state: CapabilityState, delaySeconds: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        this.clearTimer(state)
        reject(unavailable(state.abortController.signal.reason))
      }

      state.abortController.signal.addEventListener('abort', onAbort, { once: true })
      state.timer = setTimeout(() => {
        state.timer = null
        state.abortController.signal.removeEventListener('abort', onAbort)
        resolve()
      }, delaySeconds * 1_000)
      state.timer.unref?.()
    })
  }

  private scheduleRotation(state: CapabilityState): void {
    this.assertCurrent(state)
    const active = state.active
    assertActive(active)
    this.clearTimer(state)
    const delayMs = Math.max(0, (active.rotateAt - this.clock()) * 1_000)

    state.timer = setTimeout(() => {
      state.timer = null
      void this.refresh(state.binding.key, 'timer').catch(() => undefined)
    }, delayMs)
    state.timer.unref?.()
  }

  private clearTimer(state: CapabilityState): void {
    if (state.timer) {
      clearTimeout(state.timer)
      state.timer = null
    }
  }

  private expire(state: CapabilityState): void {
    const error = unavailable()

    if (!state.abortController.signal.aborted) {
      state.abortController.abort(error)
    }

    this.clearTimer(state)
    state.active = null
    state.candidate = null
    state.binding.control.close(error)
  }

  private toSnapshot(state: CapabilityState, token: AuthScopeToken): LocalCapabilitySnapshot {
    return {
      key: state.binding.key,
      bearer: token.bearer,
      registrationId: token.registrationId,
      scope: { ...token.scope },
      backendGeneration: state.binding.backendGeneration,
      issuedAt: token.issuedAt,
      rotateAt: token.rotateAt,
      validUntil: token.validUntil
    }
  }

  private diagnostic(
    state: CapabilityState,
    name: string,
    attempt: number,
    startedAt: number
  ): void {
    try {
      this.onDiagnostic({
        name,
        backendGeneration: state.binding.backendGeneration,
        attempt,
        elapsedMs: Math.max(0, (this.clock() - startedAt) * 1_000)
      })
    } catch {
      // Diagnostics are best-effort and must never affect capability state.
    }
  }
}

function assertActive(token: AuthScopeToken | null): asserts token is AuthScopeToken {
  if (!token) {
    throw unavailable()
  }
}
