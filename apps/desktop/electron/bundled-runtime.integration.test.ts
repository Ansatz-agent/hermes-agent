import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { classifyBundledRuntime, resolveBundledBootstrapRoot } from './bundled-runtime-state'

const PAYLOAD_COMMIT = 'a'.repeat(40)
const OTHER_COMMIT = 'b'.repeat(40)

test('packaged macOS and Windows resolve the bundled bootstrap resource', () => {
  assert.equal(
    resolveBundledBootstrapRoot({
      packaged: true,
      platform: 'darwin',
      resourcesPath: '/Applications/Ansatz.app/Contents/Resources'
    }),
    path.join('/Applications/Ansatz.app/Contents/Resources', 'bootstrap')
  )
  assert.equal(resolveBundledBootstrapRoot({ packaged: false, platform: 'darwin', resourcesPath: '/tmp/resources' }), null)
  assert.equal(
    resolveBundledBootstrapRoot({ packaged: true, platform: 'win32', resourcesPath: 'C:\\resources' }),
    path.join('C:\\resources', 'bootstrap')
  )
  assert.equal(resolveBundledBootstrapRoot({ packaged: true, platform: 'linux', resourcesPath: '/tmp/resources' }), null)
})

test('bundled runtime installs, reuses, and refreshes only verified desktop payloads', () => {
  assert.equal(classifyBundledRuntime({ packaged: false, runtimeUsable: false }), 'not-applicable')
  assert.equal(classifyBundledRuntime({ packaged: true, runtimeUsable: false, payloadCommit: '0'.repeat(40) }), 'payload-invalid')
  assert.equal(classifyBundledRuntime({ packaged: true, runtimeUsable: false, payloadCommit: PAYLOAD_COMMIT }), 'install')
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: 'desktop-bundle',
      sourceCommit: PAYLOAD_COMMIT,
      payloadCommit: PAYLOAD_COMMIT
    }),
    'reuse'
  )
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: 'desktop-bundle',
      sourceCommit: OTHER_COMMIT,
      payloadCommit: PAYLOAD_COMMIT
    }),
    'refresh'
  )
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: 'desktop-bundle',
      sourceCommit: OTHER_COMMIT,
      sourceOrigin: 'release-server',
      payloadCommit: PAYLOAD_COMMIT
    }),
    'reuse'
  )
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: 'source',
      payloadCommit: PAYLOAD_COMMIT
    }),
    'not-applicable'
  )
})

test('interrupted managed installs are resumed before reuse', () => {
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: 'desktop-bundle',
      sourceCommit: PAYLOAD_COMMIT,
      payloadCommit: PAYLOAD_COMMIT,
      transactionPending: true
    }),
    'refresh'
  )
  assert.equal(
    classifyBundledRuntime({
      packaged: true,
      runtimeUsable: true,
      installMethod: null,
      sourceCommit: PAYLOAD_COMMIT,
      payloadCommit: PAYLOAD_COMMIT
    }),
    'refresh'
  )
})
