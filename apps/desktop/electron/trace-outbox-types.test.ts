import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type TraceOwner, validateTraceOwner } from './trace-outbox-types'

function validOwner(): TraceOwner {
  return {
    accountKey: 'account-11111111-1111-4111-8111-111111111111',
    accountId: '11111111-1111-4111-8111-111111111111',
    sessionId: '22222222-2222-4222-8222-222222222222',
    installationId: '33333333-3333-4333-8333-333333333333'
  }
}

test('trusted Trace owners require UUID identities while legacy owners remain local-only', () => {
  assert.equal(validateTraceOwner(validOwner()).uploadable, true)
  assert.equal(
    validateTraceOwner({
      accountKey: `legacy-${'a'.repeat(64)}`,
      accountId: null,
      sessionId: null,
      installationId: '33333333-3333-4333-8333-333333333333'
    }).uploadable,
    false
  )
  assert.throws(() => validateTraceOwner({ ...validOwner(), accountKey: '../escape' }), /invalid_account_key/)
})

test('trusted Trace owner account keys exactly match their UUIDv4 account identity', () => {
  assert.equal(validateTraceOwner(validOwner()).uploadable, true)
  assert.throws(
    () =>
      validateTraceOwner({
        ...validOwner(),
        accountKey: 'account-44444444-4444-4444-8444-444444444444'
      }),
    /invalid_trace_owner/
  )
  assert.throws(
    () =>
      validateTraceOwner({
        ...validOwner(),
        accountKey: 'account-11111111-1111-3111-8111-111111111111'
      }),
    /invalid_trace_owner/
  )
})

test('trusted Trace owners reject half-null account and session identities', () => {
  assert.throws(() => validateTraceOwner({ ...validOwner(), accountId: null }), /invalid_trace_owner/)
  assert.throws(() => validateTraceOwner({ ...validOwner(), sessionId: null }), /invalid_trace_owner/)
})

test('legacy Trace owners reject trusted account or session identities', () => {
  const legacy = { ...validOwner(), accountKey: `legacy-${'a'.repeat(64)}` }

  assert.throws(() => validateTraceOwner(legacy), /invalid_trace_owner/)
  assert.throws(() => validateTraceOwner({ ...legacy, accountId: null }), /invalid_trace_owner/)
  assert.throws(() => validateTraceOwner({ ...legacy, sessionId: null }), /invalid_trace_owner/)
})
