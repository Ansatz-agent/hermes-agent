import assert from 'node:assert/strict'

import { test } from 'vitest'

import { attachTraceBackends, TraceTransportUnavailableError } from './trace-backend-attacher'

test('stale EPIPE is isolated while a live child receives the recovered transport', async () => {
  const stale = { name: 'stale' }
  const live = { name: 'live' }
  const writes: string[] = []
  const diagnostics: string[] = []

  const result = await attachTraceBackends(
    [
      { child: stale, generation: 'old', root: '/runtime/stale' },
      { child: live, generation: 'current', root: '/runtime/override' }
    ],
    async descriptor => {
      if (descriptor.child === stale) {
        throw new TraceTransportUnavailableError('trace_transport_pipe_unavailable')
      }

      writes.push(`${descriptor.generation}:${descriptor.root}`)
    },
    diagnostic => diagnostics.push(diagnostic)
  )

  assert.deepEqual(writes, ['current:/runtime/override'])
  assert.deepEqual(result, { attempted: 2, failed: 1, succeeded: 1 })
  assert.deepEqual(diagnostics, ['trace transport attach unavailable for generation old'])
})

test('unexpected child errors are redacted and never reject the attach round', async () => {
  const secret = 'Bearer ' + 's'.repeat(43)
  const diagnostics: string[] = []

  await assert.doesNotReject(() =>
    attachTraceBackends(
      [{ child: {}, generation: 'g1', root: '/runtime/live' }],
      async () => {
        throw new Error(`EPIPE ${secret}`)
      },
      diagnostic => diagnostics.push(diagnostic)
    )
  )
  assert.deepEqual(diagnostics, ['trace transport attach unavailable for generation g1'])
  assert.equal(diagnostics.join('').includes(secret), false)
})
