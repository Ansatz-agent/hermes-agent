import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { TraceCredential } from './auth-bridge'
import {
  RebindableTraceCredentialSource,
  RefreshingTraceCredentialProvider,
  type TraceCredentialSource
} from './trace-credential-provider'
import type { TraceOwner } from './trace-outbox-types'

const installationId = '11111111-1111-4111-8111-111111111111'
const now = Date.parse('2099-08-23T14:00:00Z')

function credential(accessToken: string, overrides: Partial<TraceCredential> = {}): TraceCredential {
  return {
    access_token: accessToken,
    expires_at: '2099-08-23T14:15:00Z',
    expires_in: 900,
    installation_id: installationId,
    ...overrides
  }
}

function validOwner(overrides: Partial<TraceOwner> = {}): TraceOwner {
  const accountId = overrides.accountId ?? '11111111-1111-4111-8111-111111111111'

  return {
    accountId,
    accountKey: `account-${accountId}`,
    installationId,
    sessionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    ...overrides
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

test('a same-account rebind rejects the old loader result and only caches the new binding', async () => {
  const oldFlight = deferred<TraceCredential>()
  const ownerA = validOwner({ sessionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' })
  const ownerB = validOwner({ sessionId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' })
  const source = new RebindableTraceCredentialSource()
  source.bind(ownerA, () => oldFlight.promise)
  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => now, installationId })

  const stale = provider.current()
  source.bind(ownerB, async () => credential('new-session-token-1234567890'))
  provider.invalidate()
  oldFlight.resolve(credential('old-session-token-1234567890'))

  await assert.rejects(stale, /trace_credential_binding_changed/)
  assert.equal((await provider.current()).access_token, 'new-session-token-1234567890')
  assert.deepEqual(source.owner(), ownerB)
})

test('credential source refuses cross-account rebind and keeps the active binding', async () => {
  const source = new RebindableTraceCredentialSource()
  const ownerA = validOwner()
  source.bind(ownerA, async () => credential('account-a-token-1234567890'))

  assert.throws(
    () => source.bind(validOwner({ accountId: '22222222-2222-4222-8222-222222222222' }), async () => credential('x')),
    /trace_credential_account_mismatch/
  )
  assert.deepEqual(source.owner(), ownerA)
})

test('credential source reports unavailable until an owner is bound', async () => {
  const source = new RebindableTraceCredentialSource()

  await assert.rejects(source.load(false), /trace_credential_binding_unavailable/)
  assert.equal(source.owner(), null)
})

test('clearing the credential source rejects its in-flight loader and removes the owner', async () => {
  const oldFlight = deferred<TraceCredential>()
  const source = new RebindableTraceCredentialSource()
  source.bind(validOwner(), () => oldFlight.promise)

  const stale = source.load(false)
  source.clear()
  oldFlight.resolve(credential('cleared-binding-token-1234567890'))

  await assert.rejects(stale, /trace_credential_binding_changed/)
  assert.equal(source.owner(), null)
})

test('a 401 cannot coalesce forced refresh into a stale normal flight', async () => {
  const normal = deferred<TraceCredential>()
  const calls: boolean[] = []

  const provider = new RefreshingTraceCredentialProvider(
    {
      load: force => {
        calls.push(force)

        return force ? Promise.resolve(credential('fresh-token-1234567890')) : normal.promise
      }
    },
    { clock: () => now, installationId }
  )

  const first = provider.current()
  const forced = provider.current({ forceRefresh: true })

  normal.resolve(credential('stale-token-1234567890'))

  assert.equal((await first).access_token, 'stale-token-1234567890')
  assert.equal((await forced).access_token, 'fresh-token-1234567890')
  assert.deepEqual(calls, [false, true])
})

test('coalesces concurrent normal and concurrent forced loads independently', async () => {
  const normal = deferred<TraceCredential>()
  const forced = deferred<TraceCredential>()
  const calls: boolean[] = []

  const source: TraceCredentialSource = {
    load(force) {
      calls.push(force)

      return force ? forced.promise : normal.promise
    }
  }

  const provider = new RefreshingTraceCredentialProvider(source, { clock: () => now, installationId })
  const first = provider.current()
  const second = provider.current()
  const refreshOne = provider.current({ forceRefresh: true })
  const refreshTwo = provider.current({ forceRefresh: true })

  normal.resolve(credential('normal-token-1234567890'))
  forced.resolve(credential('forced-token-1234567890'))

  assert.equal((await first).access_token, 'normal-token-1234567890')
  assert.equal((await second).access_token, 'normal-token-1234567890')
  assert.equal((await refreshOne).access_token, 'forced-token-1234567890')
  assert.equal((await refreshTwo).access_token, 'forced-token-1234567890')
  assert.deepEqual(calls, [false, true])
})

test('invalidate and clear prevent late flights from restoring a credential', async () => {
  const first = deferred<TraceCredential>()
  const second = deferred<TraceCredential>()
  const calls: boolean[] = []

  const provider = new RefreshingTraceCredentialProvider(
    {
      load(force) {
        calls.push(force)

        return calls.length === 1 ? first.promise : second.promise
      }
    },
    { clock: () => now, installationId }
  )

  const stale = provider.current()
  provider.invalidate()
  const fresh = provider.current()
  first.resolve(credential('stale-token-1234567890'))
  second.resolve(credential('fresh-token-1234567890'))

  assert.equal((await stale).access_token, 'stale-token-1234567890')
  assert.equal((await fresh).access_token, 'fresh-token-1234567890')
  provider.clear()
  assert.equal(provider.expiresAt(), null)
  assert.deepEqual(calls, [false, false])
})

test('rejects invalid credentials without including the token in its error', async () => {
  const secret = 'secret-token-should-not-appear-1234567890'

  const provider = new RefreshingTraceCredentialProvider(
    {
      load: async () => credential(secret, { installation_id: '22222222-2222-4222-8222-222222222222' })
    },
    { clock: () => now, installationId }
  )

  await assert.rejects(provider.current(), error => {
    assert.match(String(error), /trace_credential_unavailable/)
    assert.doesNotMatch(String(error), new RegExp(secret))

    return true
  })
})

test('does not return a bearer that expires at the injected provider clock', async () => {
  const provider = new RefreshingTraceCredentialProvider(
    { load: async () => credential('expired-at-now-token-1234567890', { expires_at: '2099-08-23T14:00:00Z' }) },
    { clock: () => now, installationId }
  )

  await assert.rejects(provider.current(), /trace_credential_unavailable/)
})

test('rejects impossible, far-future, and lifetime-mismatched credential expiries', async () => {
  for (const expiresAt of ['2099-02-30T14:15:00Z', '9999-08-23T14:15:00Z', '2099-08-23T14:15:30.001Z']) {
    const provider = new RefreshingTraceCredentialProvider(
      { load: async () => credential('invalid-expiry-token-1234567890', { expires_at: expiresAt }) },
      { clock: () => now, installationId }
    )

    await assert.rejects(provider.current(), /trace_credential_unavailable/)
  }
})

test('accepts expiry at the documented 30-second clock-skew boundary', async () => {
  const provider = new RefreshingTraceCredentialProvider(
    { load: async () => credential('boundary-expiry-token-1234567890', { expires_at: '2099-08-23T14:15:30Z' }) },
    { clock: () => now, installationId }
  )

  assert.equal((await provider.current()).expires_at, '2099-08-23T14:15:30Z')
})
