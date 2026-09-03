import assert from 'node:assert/strict'
import fs from 'node:fs'

import { test } from 'vitest'

const source = fs.readFileSync(new URL('./main.ts', import.meta.url), 'utf8')

function functionSource(name: string): string {
  const start = source.indexOf(`async function ${name}`)
  assert.notEqual(start, -1, `${name} must exist`)
  const next = source.indexOf('\nasync function ', start + 1)

  return source.slice(start, next === -1 ? source.length : next)
}

test('Desktop obtains an exact shared ingress lease before starting legacy recovery', () => {
  const ensure = functionSource('ensureDesktopTraceForwarder')

  assert.match(ensure, /bridge\.traceIngress\(\{ entrypoint: 'desktop', consumer_id: 'desktop-local' \}\)/)
  assert.match(ensure, /lease\.installation_id !== requestedOwner\.installationId/)
  assert.match(ensure, /desktopSharedTraceLease = lease/)
  assert.match(ensure, /desktopTraceCoordinator\.activate/)
  assert.ok(
    ensure.indexOf('desktopSharedTraceLease = lease') < ensure.indexOf('desktopTraceCoordinator.activate'),
    'the shared ingress must own all new admission before legacy recovery starts'
  )
  assert.doesNotMatch(ensure, /traceToken/)
})

test('legacy Electron ciphertext is recovery-only and never opens an admission socket', () => {
  const recovery = functionSource('createDesktopLegacyTraceRecoverySession')

  assert.match(recovery, /forwarder\.startRecovery\(owner\)/)
  assert.doesNotMatch(recovery, /forwarder\.start\(owner\)/)
  assert.match(source, /install\(_ingress\) \{\n\s*\/\/ New OTLP is admitted only by the shared auth-owner ingress lease\./)
})

test('backend Trace registration is sourced only from the shared owner lease', () => {
  const write = functionSource('writeBackendTraceTransport')

  assert.match(write, /const trace = desktopSharedTraceLease/)
  assert.match(write, /entrypoint: trace\.entrypoint/)
  assert.match(write, /pluginsToml: trace\.plugins_toml/)
  assert.doesNotMatch(write, /desktopTraceIngress/)
  assert.doesNotMatch(write, /desktopTraceRuntime/)
})
