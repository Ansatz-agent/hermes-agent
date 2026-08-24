import assert from 'node:assert/strict'

import { test } from 'vitest'

import { ANSATZ_PRODUCT, resolveAnsatzRuntimeRoot } from './ansatz-product'

test('Ansatz desktop identity cannot collide with an existing Hermes installation', () => {
	assert.equal(ANSATZ_PRODUCT.productName, 'Ansatz')
	assert.equal(ANSATZ_PRODUCT.appId, 'cn.c2sml.ansatz.voice-trace-client')
	assert.equal(ANSATZ_PRODUCT.executableName, 'Ansatz')
	assert.equal(ANSATZ_PRODUCT.artifactPrefix, 'Ansatz')
	assert.equal(ANSATZ_PRODUCT.protocolScheme, 'ansatz-voice-trace')
})

test('resolveAnsatzRuntimeRoot isolates macOS and Windows user data from Hermes', () => {
  assert.equal(
    resolveAnsatzRuntimeRoot('darwin', '/Users/a', ''),
    '/Users/a/.ansatz-voice-trace-client'
  )
  assert.equal(
    resolveAnsatzRuntimeRoot('win32', 'C:\\Users\\a', 'C:\\Users\\a\\AppData\\Local'),
    'C:\\Users\\a\\AppData\\Local\\AnsatzVoiceTraceClient'
  )
  assert.notEqual(resolveAnsatzRuntimeRoot('darwin', '/Users/a', ''), '/Users/a/.hermes')
})

test('resolveAnsatzRuntimeRoot requires LOCALAPPDATA on Windows', () => {
  assert.throws(
    () => resolveAnsatzRuntimeRoot('win32', 'C:\\Users\\a', ''),
    /LOCALAPPDATA/
  )
})
