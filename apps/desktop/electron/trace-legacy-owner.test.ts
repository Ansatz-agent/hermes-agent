import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  legacyTraceOwnerForPrincipal,
  migratePreviousLegacyTraceNamespace,
  previousLegacyTraceAccountKey
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
