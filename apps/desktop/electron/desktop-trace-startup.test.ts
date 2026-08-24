import assert from 'node:assert/strict'

import { test } from 'vitest'

import { prepareLocalTraceCapture } from './desktop-trace-startup'

test('backend preparation continues when local trace capture cannot start', async () => {
  const events: string[] = []

  const result = await prepareLocalTraceCapture({
    startListener: async () => Promise.reject(new Error('local listener unavailable')),
    onDiagnostic: message => events.push(`diagnostic:${message}`),
    scheduleRetry: () => events.push('retry')
  })

  events.push('spawn-backend')

  assert.equal(result, null)
  assert.deepEqual(events, ['diagnostic:trace capture unavailable', 'retry', 'spawn-backend'])
})

test('trace capture reports no listener error details', async () => {
  const diagnostics: string[] = []

  await prepareLocalTraceCapture({
    startListener: async () => Promise.reject(new Error('native session token must remain secret')),
    onDiagnostic: message => diagnostics.push(message),
    scheduleRetry: () => {}
  })

  assert.deepEqual(diagnostics, ['trace capture unavailable'])
})
