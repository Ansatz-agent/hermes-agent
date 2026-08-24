import assert from 'node:assert/strict'

import { test } from 'vitest'

import { EMBED_SESSION_PARTITION } from './embed-referer'

test('embed requests use the Ansatz-owned persistent partition', () => {
  assert.equal(EMBED_SESSION_PARTITION, 'persist:ansatz-voice-trace-embed')
})
