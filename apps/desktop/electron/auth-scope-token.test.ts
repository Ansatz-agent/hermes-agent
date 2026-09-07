import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'

import { test, vi } from 'vitest'

import {
  AUTH_SCOPE_TOKEN_OVERLAP_SECONDS,
  AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS,
  AUTH_SCOPE_TOKEN_TTL_SECONDS,
  DESKTOP_SCOPE_PROTOCOL_VERSION,
  encodeScopeTokenPromotion,
  encodeScopeTokenRegistration,
  encodeTraceTransportRegistration,
  issueAuthScopeToken,
  issueScopeTransitionId,
  sanitizeAnsatzAuthChildEnvironment,
  sanitizeAuthChildEnvironment
} from './auth-scope-token'

const scope = {
  connection_id: 'local',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  epoch: 7
}

test('issues a 30-minute v2 candidate and rotates after 20 minutes', () => {
  const randomBytes = vi.fn((size: number) => Buffer.alloc(size, 0xa5))
  const randomIdBytes = vi.fn((size: number) => Buffer.alloc(size, 0x5a))
  const token = issueAuthScopeToken(scope, { clock: () => 100, randomBytes, randomIdBytes })

  assert.equal(randomBytes.mock.calls[0]?.[0], 32)
  assert.equal(randomIdBytes.mock.calls[0]?.[0], 16)
  assert.equal(Buffer.from(token.bearer, 'base64url').byteLength, 32)
  assert.equal(Buffer.from(token.registrationId, 'base64url').byteLength, 16)
  assert.deepEqual(token.scope, scope)
  assert.equal(token.ttlSeconds, 1_800)
  assert.equal(token.issuedAt, 100)
  assert.equal(token.rotateAt, 1_300)
  assert.equal(token.validUntil, 1_900)
  assert.equal(AUTH_SCOPE_TOKEN_TTL_SECONDS, 1_800)
  assert.equal(AUTH_SCOPE_TOKEN_ROTATE_AFTER_SECONDS, 1_200)
  assert.equal(AUTH_SCOPE_TOKEN_OVERLAP_SECONDS, 60)
  assert.equal(DESKTOP_SCOPE_PROTOCOL_VERSION, 2)
})

test('never treats a visible scope tuple as the bearer', () => {
  const first = issueAuthScopeToken(scope, { clock: () => 100 })
  const second = issueAuthScopeToken(scope, { clock: () => 100 })

  assert.notEqual(first.bearer, second.bearer)
  assert.notEqual(first.bearer, `${scope.connection_id}:${scope.runtime_instance_id}:${scope.epoch}`)
})

test('encodes strict v2 register and promote frames inside bounded stdin frames', () => {
  const token = issueAuthScopeToken(scope, {
    clock: () => 100,
    randomBytes: size => Buffer.alloc(size, 0xa5),
    randomIdBytes: size => Buffer.alloc(size, 0x5a)
  })

  const transitionId = issueScopeTransitionId(size => Buffer.alloc(size, 0x33))

  const registration = encodeScopeTokenRegistration(token)
  const promotion = encodeScopeTokenPromotion(token, null, transitionId)

  assert.equal(registration.endsWith('\n'), true)
  assert.ok(Buffer.byteLength(registration) <= 4_096)
  assert.deepEqual(JSON.parse(registration), {
    version: 2,
    operation: 'register_scope_token',
    registration_id: token.registrationId,
    bearer: token.bearer,
    connection_id: 'local',
    runtime_instance_id: '0123456789abcdef0123456789abcdef',
    epoch: 7,
    ttl_seconds: 1_800
  })
  assert.equal(promotion.endsWith('\n'), true)
  assert.ok(Buffer.byteLength(promotion) <= 4_096)
  assert.deepEqual(JSON.parse(promotion), {
    version: 2,
    operation: 'promote_scope_token',
    transition_id: transitionId,
    registration_id: token.registrationId,
    previous_registration_id: null,
    connection_id: 'local',
    runtime_instance_id: '0123456789abcdef0123456789abcdef',
    epoch: 7,
    overlap_seconds: 60
  })
})

test('serializes dynamic Trace transport only inside the bounded main-to-backend control frame', () => {
  const encoded = encodeTraceTransportRegistration({
    endpoint: 'http://127.0.0.1:49152/v1/traces',
    entrypoint: 'desktop',
    installationId: '11111111-1111-4111-8111-111111111111',
    localBearer: 'a'.repeat(43),
    pluginsToml: '/Applications/Ansatz.app/Contents/Resources/config/ansatz-voice-trace/plugins.toml'
  })

  assert.deepEqual(JSON.parse(encoded.trim()), {
    authorization: `Bearer ${'a'.repeat(43)}`,
    endpoint: 'http://127.0.0.1:49152/v1/traces',
    entrypoint: 'desktop',
    installation_id: '11111111-1111-4111-8111-111111111111',
    operation: 'register_trace_transport',
    plugins_toml: '/Applications/Ansatz.app/Contents/Resources/config/ansatz-voice-trace/plugins.toml',
    version: 1
  })
  assert.ok(Buffer.byteLength(encoded) <= 4_096)
})

test('rejects a Trace transport registration without an explicit desktop entrypoint', () => {
  assert.throws(
    () =>
      encodeTraceTransportRegistration({
        endpoint: 'http://127.0.0.1:49152/v1/traces',
        installationId: '11111111-1111-4111-8111-111111111111',
        localBearer: 'a'.repeat(43),
        pluginsToml: '/Applications/Ansatz.app/Contents/Resources/config/ansatz-voice-trace/plugins.toml'
      } as never),
    /Invalid Trace transport registration/
  )
})

test('rejects malformed scopes and entropy before producing a candidate', () => {
  const randomBytes = vi.fn((_size: number) => Buffer.alloc(32))
  const randomIdBytes = vi.fn((_size: number) => Buffer.alloc(16))

  assert.throws(() => issueAuthScopeToken({ ...scope, connection_id: '' }, { randomBytes, randomIdBytes }), /scope/i)
  assert.equal(randomBytes.mock.calls.length, 0)
  assert.equal(randomIdBytes.mock.calls.length, 0)

  assert.throws(
    () =>
      issueAuthScopeToken(scope, {
        clock: () => 100,
        randomBytes: () => Buffer.alloc(31),
        randomIdBytes
      }),
    /entropy/i
  )
  assert.throws(
    () =>
      issueAuthScopeToken(scope, {
        clock: () => 100,
        randomBytes,
        randomIdBytes: () => Buffer.alloc(15)
      }),
    /registration id/i
  )
})

test('strips inherited auth credentials from backend and PTY child environments', () => {
  const sanitized = sanitizeAuthChildEnvironment({
    HERMES_AUTH_SCOPE_TOKEN: 'scope-secret',
    HERMES_DASHBOARD_SESSION_TOKEN: 'legacy-secret',
    HERMES_HOME: '/tmp/hermes',
    PATH: '/usr/bin'
  })

  assert.deepEqual(sanitized, {
    HERMES_HOME: '/tmp/hermes',
    PATH: '/usr/bin'
  })
})

test('pins local backend children to the same non-legacy Ansatz auth owner as the bridge', () => {
  const sanitized = sanitizeAnsatzAuthChildEnvironment(
    {
      HERMES_AUTH_SCOPE_TOKEN: 'scope-secret',
      HERMES_DASHBOARD_SESSION_TOKEN: 'legacy-secret',
      HERMES_AUTH_RUNTIME_NAMESPACE: 'stale-owner',
      HERMES_AUTH_KEYRING_SERVICE: 'stale-service',
      HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'must-not-be-read-automatically',
      PATH: '/usr/bin'
    },
    '/Users/a/.ansatz-voice-trace-client'
  )

  assert.deepEqual(sanitized, {
    HERMES_AUTH_RUNTIME_NAMESPACE: 'ansatz-voice-trace-client-auth-v2',
    HERMES_AUTH_KEYRING_SERVICE: 'cn.c2sml.ansatz.voice-trace-client.remote-auth',
    HERMES_HOME: '/Users/a/.ansatz-voice-trace-client',
    PATH: '/usr/bin'
  })
})
