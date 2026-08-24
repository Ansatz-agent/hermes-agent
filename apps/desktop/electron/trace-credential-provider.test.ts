import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { TraceCredential } from './auth-bridge'
import { RefreshingTraceCredentialProvider, type TraceCredentialSource } from './trace-credential-provider'

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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject, resolve }
}

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
