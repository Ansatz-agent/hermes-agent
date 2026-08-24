import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { removeLegacyAnsatzPartitions } from './legacy-product-cleanup'

const temporaryRoots: string[] = []

function fixtureRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ansatz-legacy-partitions-'))
  temporaryRoots.push(root)
  fs.mkdirSync(path.join(root, 'Partitions'), { recursive: true })
  return root
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

test('removes only exact legacy partition directories under Ansatz userData', async () => {
  const root = fixtureRoot()
  const partitions = path.join(root, 'Partitions')

  for (const name of [
    'hermes-embed',
    'hermes-preview',
    'hermes-remote-oauth',
    'persist:hermes-preview-extra',
    'ansatz-voice-trace-preview'
  ]) {
    fs.mkdirSync(path.join(partitions, name))
    fs.writeFileSync(path.join(partitions, name, 'sentinel'), name)
  }

  const removed = await removeLegacyAnsatzPartitions(root)

  assert.deepEqual(removed, ['hermes-embed', 'hermes-preview', 'hermes-remote-oauth'])
  assert.equal(fs.existsSync(path.join(partitions, 'hermes-embed')), false)
  assert.equal(fs.existsSync(path.join(partitions, 'hermes-preview')), false)
  assert.equal(fs.existsSync(path.join(partitions, 'hermes-remote-oauth')), false)
  assert.equal(fs.existsSync(path.join(partitions, 'persist:hermes-preview-extra', 'sentinel')), true)
  assert.equal(fs.existsSync(path.join(partitions, 'ansatz-voice-trace-preview', 'sentinel')), true)
})

test('refuses symlinked partition roots and symlinked legacy targets', async () => {
  const root = fixtureRoot()
  const partitions = path.join(root, 'Partitions')
  const outside = path.join(root, 'outside')
  fs.mkdirSync(outside)
  fs.writeFileSync(path.join(outside, 'sentinel'), 'keep')
  fs.symlinkSync(outside, path.join(partitions, 'hermes-preview'))

  assert.deepEqual(await removeLegacyAnsatzPartitions(root), [])
  assert.equal(fs.lstatSync(path.join(partitions, 'hermes-preview')).isSymbolicLink(), true)
  assert.equal(fs.readFileSync(path.join(outside, 'sentinel'), 'utf8'), 'keep')

  fs.rmSync(partitions, { force: true, recursive: true })
  fs.symlinkSync(outside, partitions)

  assert.deepEqual(await removeLegacyAnsatzPartitions(root), [])
  assert.equal(fs.readFileSync(path.join(outside, 'sentinel'), 'utf8'), 'keep')
})
