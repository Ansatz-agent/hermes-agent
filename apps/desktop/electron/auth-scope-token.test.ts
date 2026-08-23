import assert from 'node:assert/strict'
import { Buffer } from 'node:buffer'

import { test, vi } from 'vitest'

import {
  encodeScopeTokenRegistration,
  issueAuthScopeToken,
  sanitizeAuthChildEnvironment
} from './auth-scope-token'

const scope = {
  connection_id: 'local',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  epoch: 7
}

test('issues at least 256 random bits bound to the exact scope for at most 60 seconds', () => {
  const randomBytes = vi.fn((_size: number) => Buffer.alloc(32, 0xa5))
  const token = issueAuthScopeToken(scope, { clock: () => 100, randomBytes })

  assert.equal(randomBytes.mock.calls[0]?.[0], 32)
  assert.equal(Buffer.from(token.bearer, 'base64url').byteLength, 32)
  assert.deepEqual(token.scope, scope)
  assert.equal(token.validUntil, 160)
})

test('never treats a visible scope tuple as the bearer', () => {
  const first = issueAuthScopeToken(scope, { clock: () => 100 })
  const second = issueAuthScopeToken(scope, { clock: () => 100 })

  assert.notEqual(first.bearer, second.bearer)
  assert.notEqual(first.bearer, `${scope.connection_id}:${scope.runtime_instance_id}:${scope.epoch}`)
})

test('serializes the bearer only inside a bounded stdin registration frame', () => {
  const token = issueAuthScopeToken(scope, {
    clock: () => 100,
    randomBytes: () => Buffer.alloc(32, 0x5a),
    ttlSeconds: 45
  })

  const encoded = encodeScopeTokenRegistration(token)

  const frame = JSON.parse(encoded.trim())

  assert.equal(encoded.endsWith('\n'), true)
  assert.ok(Buffer.byteLength(encoded) <= 4_096)
  assert.deepEqual(frame, {
    bearer: token.bearer,
    connection_id: 'local',
    epoch: 7,
    operation: 'register_scope_token',
    runtime_instance_id: '0123456789abcdef0123456789abcdef',
    ttl_seconds: 45,
    version: 1
  })
})

test('rejects oversized TTLs and malformed scopes before producing a secret', () => {
  const randomBytes = vi.fn((_size: number) => Buffer.alloc(32))

  assert.throws(() => issueAuthScopeToken(scope, { randomBytes, ttlSeconds: 61 }), /TTL/)
  assert.throws(() => issueAuthScopeToken({ ...scope, connection_id: '' }, { randomBytes }), /scope/i)
  assert.equal(randomBytes.mock.calls.length, 0)
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
