import assert from 'node:assert/strict'

import { test } from 'vitest'

import { runWithFixtureCleanup } from '../e2e/authenticated-fixture-runner'

test('primary failure remains first and cause when cleanup has multiple failures', async () => {
  const primary = new Error('primary assertion failed')
  const cleanupOne = new Error('desktop cleanup failed')
  const cleanupTwo = new Error('auth cleanup failed')
  const calls: string[] = []
  let thrown: unknown

  try {
    await runWithFixtureCleanup(
      async () => {
        calls.push('body')
        throw primary
      },
      async () => {
        calls.push('cleanup')
        const settled = await Promise.allSettled([
          Promise.resolve().then(() => {
            calls.push('desktop')
            throw cleanupOne
          }),
          Promise.resolve().then(() => {
            calls.push('auth')
            throw cleanupTwo
          }),
          Promise.resolve().then(() => calls.push('sandbox'))
        ])
        throw new AggregateError(
          settled.flatMap(result => (result.status === 'rejected' ? [result.reason] : [])),
          'fixture cleanup failed'
        )
      }
    )
  } catch (error) {
    thrown = error
  }

  assert.ok(thrown instanceof AggregateError)
  assert.equal(thrown.cause, primary)
  assert.deepEqual(thrown.errors, [primary, cleanupOne, cleanupTwo])
  assert.deepEqual(calls, ['body', 'cleanup', 'desktop', 'auth', 'sandbox'])
})

test('primary-only failure is rethrown unchanged', async () => {
  const primary = new Error('primary assertion failed')

  await assert.rejects(
    runWithFixtureCleanup(
      async () => Promise.reject(primary),
      async () => {}
    ),
    error => error === primary
  )
})

test('cleanup-only failure is always reported as an aggregate', async () => {
  const cleanup = new Error('cleanup failed')
  let thrown: unknown

  try {
    await runWithFixtureCleanup(
      async () => 'result',
      async () => Promise.reject(cleanup)
    )
  } catch (error) {
    thrown = error
  }

  assert.ok(thrown instanceof AggregateError)
  assert.deepEqual(thrown.errors, [cleanup])
  assert.equal(thrown.cause, undefined)
})

test('successful cleanup preserves the body result', async () => {
  assert.equal(
    await runWithFixtureCleanup(
      async () => 'result',
      async () => {}
    ),
    'result'
  )
})
