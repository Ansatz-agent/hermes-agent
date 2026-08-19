import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { archiveEntryIsSafe, prepareBundledSource, resolveBundledPayload } from './bootstrap-payload'

const COMMIT = 'c'.repeat(40)

function sha256(filePath: string): string {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function makePayloadFixture({ symlink = false }: { symlink?: boolean } = {}) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bootstrap-payload-'))
  const sourceRoot = path.join(tempRoot, 'source', 'hermes-agent')
  const bootstrapRoot = path.join(tempRoot, 'bootstrap')
  fs.mkdirSync(path.join(sourceRoot, 'hermes_cli'), { recursive: true })
  fs.mkdirSync(path.join(sourceRoot, 'tools'), { recursive: true })
  fs.mkdirSync(path.join(sourceRoot, 'scripts'), { recursive: true })
  fs.mkdirSync(bootstrapRoot, { recursive: true })
  fs.writeFileSync(path.join(sourceRoot, 'pyproject.toml'), '[project]\nname = "fixture"\n')
  fs.writeFileSync(path.join(sourceRoot, 'hermes_cli', 'main.py'), 'def main(): pass\n')
  fs.writeFileSync(path.join(sourceRoot, 'tools', 'sensevoice_stt.py'), 'def transcribe(): pass\n')
  fs.writeFileSync(path.join(sourceRoot, 'scripts', 'hermes-gateway'), '#!/bin/sh\n')

  if (symlink) {
    fs.symlinkSync('pyproject.toml', path.join(sourceRoot, 'linked-project'))
  }

  const archivePath = path.join(bootstrapRoot, 'hermes-backend.tar.gz')
  const installerPath = path.join(bootstrapRoot, 'install.sh')
  execFileSync('tar', ['-czf', archivePath, '-C', path.join(tempRoot, 'source'), 'hermes-agent'])
  fs.writeFileSync(installerPath, '#!/bin/sh\nexit 0\n')

  const manifest = {
    schemaVersion: 1,
    commit: COMMIT,
    branch: 'integration/desktop-dmg-auth-e2e',
    archive: { file: 'hermes-backend.tar.gz', size: fs.statSync(archivePath).size, sha256: sha256(archivePath) },
    installer: { file: 'install.sh', size: fs.statSync(installerPath).size, sha256: sha256(installerPath) }
  }

  fs.writeFileSync(path.join(bootstrapRoot, 'payload-manifest.json'), JSON.stringify(manifest))

  return { tempRoot, bootstrapRoot, archivePath, installerPath }
}

test('archive path contract rejects traversal, absolute, Windows, and NUL entries', () => {
  assert.equal(archiveEntryIsSafe('hermes-agent/hermes_cli/main.py'), true)
  assert.equal(archiveEntryIsSafe('hermes-agent/../secret'), false)
  assert.equal(archiveEntryIsSafe('/hermes-agent/main.py'), false)
  assert.equal(archiveEntryIsSafe('hermes-agent\\main.py'), false)
  assert.equal(archiveEntryIsSafe('hermes-agent/secret\0.py'), false)
})

test('verified payload resolves and stages a complete managed source transaction', async () => {
  const fixture = makePayloadFixture()

  try {
    const payload = await resolveBundledPayload({ bootstrapRoot: fixture.bootstrapRoot, installStamp: { commit: COMMIT } })
    const hermesHome = path.join(fixture.tempRoot, 'home')
    const activeRoot = path.join(hermesHome, 'hermes-agent')
    const prepared = await prepareBundledSource({ payload, activeRoot, hermesHome })

    assert.equal(prepared.kind, 'fresh')
    assert.equal(fs.existsSync(path.join(activeRoot, 'hermes_cli', 'main.py')), true)
    assert.equal(fs.existsSync(path.join(activeRoot, 'scripts', 'install.sh')), true)
    assert.equal(JSON.parse(fs.readFileSync(prepared.markerPath, 'utf8')).commit, COMMIT)
    await prepared.finalize()
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('payload checksum tampering fails before extraction', async () => {
  const fixture = makePayloadFixture()

  try {
    fs.appendFileSync(fixture.installerPath, '# tampered\n')
    await assert.rejects(
      resolveBundledPayload({ bootstrapRoot: fixture.bootstrapRoot, installStamp: { commit: COMMIT } }),
      /size mismatch|checksum mismatch/
    )
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('payload archives containing symbolic links are rejected', async () => {
  const fixture = makePayloadFixture({ symlink: true })

  try {
    await assert.rejects(
      resolveBundledPayload({ bootstrapRoot: fixture.bootstrapRoot, installStamp: { commit: COMMIT } }),
      /unsupported link or entry type/
    )
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})
