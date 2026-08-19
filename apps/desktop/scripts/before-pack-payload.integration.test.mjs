import assert from 'node:assert/strict'

import { afterEach, test } from 'vitest'

import {
  ensureBundledBackendPayload,
  resetBundledBackendPayloadForTests
} from './before-pack.mjs'

afterEach(() => resetBundledBackendPayloadForTests())

test('backend payload generation is skipped outside macOS packaging', async () => {
  let calls = 0
  const build = () => { calls += 1 }

  assert.equal(await ensureBundledBackendPayload('linux', build), false)
  assert.equal(await ensureBundledBackendPayload('win32', build), false)
  assert.equal(calls, 0)
})

test('concurrent macOS beforePack hooks share one verified payload build', async () => {
  let calls = 0
  const build = async () => {
    calls += 1
    await Promise.resolve()
  }

  const results = await Promise.all([
    ensureBundledBackendPayload('darwin', build),
    ensureBundledBackendPayload('darwin', build)
  ])

  assert.deepEqual(results, [true, true])
  assert.equal(calls, 1)
})
