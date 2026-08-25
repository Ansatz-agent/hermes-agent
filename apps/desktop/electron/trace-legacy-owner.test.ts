import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  legacyTraceOwnerForPrincipal,
  localOnlyTraceOwnerForPrincipal,
  migratePreviousLegacyTraceNamespace,
  previousLegacyTraceAccountKey,
  traceMigrationSourceOwner,
  traceOwnerFromScope
} from './trace-legacy-owner'

const installationId = '11111111-1111-4111-8111-111111111111'

test('credential principal ownership is stable across runtime and epoch changes and isolates accounts', () => {
  const principal = `legacy:${'a'.repeat(64)}`
  const beforeRestart = legacyTraceOwnerForPrincipal(principal, installationId)
  const afterRestart = legacyTraceOwnerForPrincipal(principal, installationId)
  const otherAccount = legacyTraceOwnerForPrincipal(`legacy:${'b'.repeat(64)}`, installationId)

  assert.deepEqual(afterRestart, beforeRestart)
  assert.notEqual(otherAccount.accountKey, beforeRestart.accountKey)
  assert.equal(beforeRestart.accountId, null)
  assert.equal(beforeRestart.sessionId, null)
})

test('legacy Trace ownership rejects usernames, installation-only values, and malformed fingerprints', () => {
  for (const candidate of ['alice', installationId, `account:${'a'.repeat(64)}`, `legacy:${'a'.repeat(63)}`]) {
    assert.throws(() => legacyTraceOwnerForPrincipal(candidate, installationId), /invalid_legacy_principal_key/)
  }
})

test('native auth principals keep a stable local-only outbox seam until trusted Task 19 mapping', () => {
  const accountPrincipal = 'account:22222222-2222-4222-8222-222222222222'
  const first = localOnlyTraceOwnerForPrincipal(accountPrincipal, installationId)
  const restarted = localOnlyTraceOwnerForPrincipal(accountPrincipal, installationId)
  const other = localOnlyTraceOwnerForPrincipal('account:33333333-3333-4333-8333-333333333333', installationId)

  assert.deepEqual(restarted, first)
  assert.notEqual(other.accountKey, first.accountKey)
  assert.equal(first.accountId, null)
  assert.equal(first.sessionId, null)
})

test('exact native cached authorization produces a trusted uploadable owner without username inference', () => {
  const accountId = '22222222-2222-4222-8222-222222222222'
  const sessionId = '33333333-3333-4333-8333-333333333333'
  const scope = { connection_id: 'local', epoch: 7, runtime_instance_id: 'runtime-native' }
  const status = {
    account_id: accountId,
    epoch: scope.epoch,
    installation_id: installationId,
    legacy: false,
    principal_key: `account:${accountId}`,
    runtime_instance_id: scope.runtime_instance_id,
    session_id: sessionId,
    state: 'authenticated' as const,
    username: 'must-not-be-an-owner-input'
  }

  assert.deepEqual(traceOwnerFromScope(status, scope, installationId), {
    accountId,
    accountKey: `account-${accountId}`,
    installationId,
    sessionId
  })
})

test('legacy, missing, or inconsistent cached identity remains stable local-only', () => {
  const scope = { connection_id: 'local', epoch: 7, runtime_instance_id: 'runtime-native' }
  const accountId = '22222222-2222-4222-8222-222222222222'
  const base = {
    account_id: accountId,
    epoch: scope.epoch,
    installation_id: installationId,
    legacy: false,
    principal_key: `account:${accountId}`,
    runtime_instance_id: scope.runtime_instance_id,
    session_id: '33333333-3333-4333-8333-333333333333',
    state: 'authenticated' as const,
    username: 'alice'
  }

  for (const status of [
    { ...base, legacy: true },
    { ...base, session_id: null },
    { ...base, installation_id: '44444444-4444-4444-8444-444444444444' },
    { ...base, principal_key: `account:55555555-5555-4555-8555-555555555555` }
  ]) {
    const owner = traceOwnerFromScope(status, scope, installationId)
    assert.match(owner.accountKey, /^legacy-/)
    assert.equal(owner.accountId, null)
    assert.equal(owner.sessionId, null)
  }

  const missingA = traceOwnerFromScope({ ...base, principal_key: null }, scope, installationId)
  const missingB = traceOwnerFromScope({ ...base, principal_key: null, username: 'bob' }, scope, installationId)
  assert.deepEqual(missingB, missingA)
})

test('the exact previous scope namespace is atomically retained under the stable principal owner', async () => {
  const renamed: string[][] = []

  const previous = previousLegacyTraceAccountKey(
    { connection_id: 'local', epoch: 7, runtime_instance_id: 'runtime-before-upgrade' },
    installationId
  )

  const current = legacyTraceOwnerForPrincipal(`legacy:${'c'.repeat(64)}`, installationId).accountKey

  await migratePreviousLegacyTraceNamespace({
    currentAccountKey: current,
    previousAccountKey: previous,
    rename: async (source, destination) => {
      renamed.push([source, destination])
    },
    root: '/encrypted-trace-outbox'
  })

  assert.deepEqual(renamed, [[`/encrypted-trace-outbox/${previous}`, `/encrypted-trace-outbox/${current}`]])
})

test('restarted native authorization discovers the durable legacy predecessor without in-memory transition state', () => {
  const scope = { connection_id: 'local', epoch: 7, runtime_instance_id: 'runtime-transition' }
  const legacyStatus = {
    account_id: null,
    epoch: scope.epoch,
    installation_id: null,
    legacy: true,
    principal_key: `legacy:${'d'.repeat(64)}`,
    runtime_instance_id: scope.runtime_instance_id,
    session_id: null,
    state: 'authenticated'
  }
  const accountId = '22222222-2222-4222-8222-222222222222'
  const nativeStatus = {
    ...legacyStatus,
    account_id: accountId,
    installation_id: installationId,
    legacy: false,
    principal_key: `account:${accountId}`,
    session_id: '33333333-3333-4333-8333-333333333333'
  }

  const restoredNative = { ...nativeStatus, predecessor_principal_key: legacyStatus.principal_key }
  const source = traceMigrationSourceOwner(restoredNative, installationId)

  assert.deepEqual(source, legacyTraceOwnerForPrincipal(legacyStatus.principal_key, installationId))
  assert.notEqual(
    source?.accountKey,
    localOnlyTraceOwnerForPrincipal(nativeStatus.principal_key, installationId).accountKey
  )
})
