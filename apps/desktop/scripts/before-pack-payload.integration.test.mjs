import assert from 'node:assert/strict'

import { afterEach, test } from 'vitest'

import {
  ensureBundledMacPayloads,
  resetBundledBackendPayloadForTests
} from './before-pack.mjs'

afterEach(() => resetBundledBackendPayloadForTests())

test('bundled payload generation is skipped outside macOS packaging', async () => {
  let backendCalls = 0
  let authCalls = 0
  const builders = {
    buildBackend: () => { backendCalls += 1 },
    buildAuth: () => { authCalls += 1 }
  }

  assert.equal(await ensureBundledMacPayloads('linux', builders), false)
  assert.equal(await ensureBundledMacPayloads('win32', builders), false)
  assert.equal(backendCalls, 0)
  assert.equal(authCalls, 0)
})

test('concurrent macOS beforePack hooks share both verified payload builds', async () => {
  let backendCalls = 0
  let authCalls = 0
  const builders = {
    buildBackend: async () => {
      backendCalls += 1
      await Promise.resolve()
    },
    buildAuth: async () => {
      authCalls += 1
      await Promise.resolve()
    }
  }

  const results = await Promise.all([
    ensureBundledMacPayloads('darwin', builders),
    ensureBundledMacPayloads('darwin', builders)
  ])

  assert.deepEqual(results, [true, true])
  assert.equal(backendCalls, 1)
  assert.equal(authCalls, 1)
})
