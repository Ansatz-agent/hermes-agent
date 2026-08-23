import assert from 'node:assert/strict'

import { test } from 'vitest'

import { DesktopRuntimeGate } from './desktop-runtime-gate'
import { prepareInstallFirstRuntime } from './install-first-runtime'

test('complete runtime preparation finishes before the authentication bridge starts', async () => {
  const events: string[] = []
  const runtimeGate = new DesktopRuntimeGate()
  const request = { kind: 'bootstrap-needed' }

  await prepareInstallFirstRuntime({
    resolveBackend: () => {
      events.push('resolve-complete-runtime')

      return request
    },
    ensureRuntime: async (candidate, options) => {
      assert.equal(candidate, request)
      events.push(`ensure-runtime:${options.scope}`)
    },
    runtimeGate,
    startAuthBridge: async () => {
      assert.equal(runtimeGate.ready, true)
      events.push('mark-runtime-ready')
      events.push('start-auth-bridge')
    }
  })

  assert.deepEqual(events, [
    'resolve-complete-runtime',
    'ensure-runtime:runtime',
    'mark-runtime-ready',
    'start-auth-bridge'
  ])
})

test('an already usable complete runtime skips bootstrap but still opens authentication', async () => {
  const events: string[] = []
  const runtimeGate = new DesktopRuntimeGate()

  await prepareInstallFirstRuntime({
    resolveBackend: () => {
      events.push('resolve-complete-runtime')

      return null
    },
    ensureRuntime: async () => {
      events.push('unexpected-bootstrap')
    },
    runtimeGate,
    startAuthBridge: async () => {
      assert.equal(runtimeGate.ready, true)
      events.push('start-auth-bridge')
    }
  })

  assert.deepEqual(events, ['resolve-complete-runtime', 'start-auth-bridge'])
})

test('failed runtime preparation blocks auth startup and remains retryable', async () => {
  const events: string[] = []
  const runtimeGate = new DesktopRuntimeGate()
  let shouldFail = true

  const prepare = () =>
    prepareInstallFirstRuntime({
      resolveBackend: () => ({ kind: 'bootstrap-needed' }),
      ensureRuntime: async (_candidate, options) => {
        events.push(`ensure-runtime:${options.scope}`)

        if (shouldFail) {
          throw new Error('runtime failed')
        }
      },
      runtimeGate,
      startAuthBridge: async () => {
        events.push('start-auth-bridge')
      }
    })

  await assert.rejects(prepare(), /runtime failed/)
  assert.equal(runtimeGate.state, 'failed')
  assert.deepEqual(events, ['ensure-runtime:runtime'])

  shouldFail = false
  await prepare()
  assert.equal(runtimeGate.ready, true)
  assert.deepEqual(events, ['ensure-runtime:runtime', 'ensure-runtime:runtime', 'start-auth-bridge'])
})
