import assert from 'node:assert/strict'

import { test } from 'vitest'

import { encodeTraceTransportRegistration } from './auth-scope-token'
import { TraceBackendRegistry } from './trace-backend-registry'

test('recovered facade attaches each live generation with its actual override root', () => {
  const registry = new TraceBackendRegistry<object>()
  const child = {}

  registry.register(child, { generation: 'override-generation', root: '/dev/override-hermes' })
  assert.deepEqual(registry.active(), [{ child, generation: 'override-generation', root: '/dev/override-hermes' }])

  const recovered = registry.active()[0]

  const frame = JSON.parse(
    encodeTraceTransportRegistration({
      endpoint: 'http://127.0.0.1:49152/v1/traces',
      entrypoint: 'desktop',
      installationId: '11111111-1111-4111-8111-111111111111',
      localBearer: 'a'.repeat(43),
      pluginsToml: `${recovered.root}/config/ansatz-voice-trace/plugins.toml`
    })
  )

  assert.equal(frame.plugins_toml, '/dev/override-hermes/config/ansatz-voice-trace/plugins.toml')

  assert.equal(registry.unregister(child, 'stale-generation'), false)
  assert.equal(registry.active().length, 1)
  assert.equal(registry.unregister(child, 'override-generation'), true)
  assert.deepEqual(registry.active(), [])
})

test('descriptors without an actual backend root are never assigned the active install root', () => {
  const registry = new TraceBackendRegistry<object>()

  assert.throws(
    () => registry.register({}, { generation: 'external-command', root: null }),
    /trace_backend_root_unavailable/
  )
  assert.deepEqual(registry.active(), [])
})
