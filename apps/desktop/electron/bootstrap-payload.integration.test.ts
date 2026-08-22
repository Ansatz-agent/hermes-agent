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

function makePayloadFixture({
  symlink = false,
  androidHelper = false,
  githubWorkflow = false,
  platform = 'darwin'
}: { symlink?: boolean; androidHelper?: boolean; githubWorkflow?: boolean; platform?: 'darwin' | 'win32' } = {}) {
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

  if (androidHelper) {
    fs.writeFileSync(path.join(sourceRoot, 'scripts', 'install_psutil_android.py'), 'raise SystemExit(0)\n')
  }

  if (githubWorkflow) {
    fs.mkdirSync(path.join(sourceRoot, '.github', 'workflows'), { recursive: true })
    fs.writeFileSync(path.join(sourceRoot, '.github', 'workflows', 'desktop-dmg.yml'), 'name: CI only\n')
  }

  const archivePath = path.join(bootstrapRoot, 'hermes-backend.tar.gz')
  const installerFile = platform === 'win32' ? 'install.ps1' : 'install.sh'
  const installerPath = path.join(bootstrapRoot, installerFile)
  const gitRuntimePath = path.join(bootstrapRoot, 'git-bash-runtime.tar.xz')
  execFileSync('tar', ['-czf', archivePath, '-C', path.join(tempRoot, 'source'), 'hermes-agent'])
  fs.writeFileSync(installerPath, platform === 'win32' ? 'exit 0\r\n' : '#!/bin/sh\nexit 0\n')
  if (platform === 'win32') {
    fs.writeFileSync(gitRuntimePath, 'fixture Git Bash runtime')
  }

  const manifest = {
    schemaVersion: 1,
    commit: COMMIT,
    branch: 'integration/desktop-dmg-auth-e2e',
    archive: { file: 'hermes-backend.tar.gz', size: fs.statSync(archivePath).size, sha256: sha256(archivePath) },
    installer: { file: installerFile, size: fs.statSync(installerPath).size, sha256: sha256(installerPath) },
    ...(platform === 'win32'
      ? {
          gitBashRuntime: {
            file: 'git-bash-runtime.tar.xz',
            size: fs.statSync(gitRuntimePath).size,
            sha256: sha256(gitRuntimePath),
            entries: 4,
            source: {
              file: 'PortableGit-2.55.0.3-64-bit.7z.exe',
              sha256: 'ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d'
            }
          }
        }
      : {})
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

test('verified Windows payload requires and stages install.ps1', async () => {
  const fixture = makePayloadFixture({ platform: 'win32' })

  try {
    const payload = await resolveBundledPayload({
      bootstrapRoot: fixture.bootstrapRoot,
      installStamp: { commit: COMMIT },
      targetPlatform: 'win32'
    })
    const hermesHome = path.join(fixture.tempRoot, 'home')
    const activeRoot = path.join(hermesHome, 'hermes-agent')
    const prepared = await prepareBundledSource({ payload, activeRoot, hermesHome })

    assert.equal(payload.manifest.installer.file, 'install.ps1')
    assert.equal(payload.gitRuntimePath, path.join(fixture.bootstrapRoot, 'git-bash-runtime.tar.xz'))
    assert.equal(fs.readFileSync(path.join(activeRoot, 'scripts', 'install.ps1'), 'utf8'), 'exit 0\r\n')
    await prepared.finalize()
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
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

test('payload archives containing the Android-only installer are rejected on Desktop', async () => {
  const fixture = makePayloadFixture({ androidHelper: true })

  try {
    await assert.rejects(
      resolveBundledPayload({ bootstrapRoot: fixture.bootstrapRoot, installStamp: { commit: COMMIT } }),
      /unsafe entry.*install_psutil_android\.py/
    )
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('payload archives containing GitHub metadata are rejected on Desktop', async () => {
  const fixture = makePayloadFixture({ githubWorkflow: true })

  try {
    await assert.rejects(
      resolveBundledPayload({ bootstrapRoot: fixture.bootstrapRoot, installStamp: { commit: COMMIT } }),
      /unsafe entry.*\.github\//
    )
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})
