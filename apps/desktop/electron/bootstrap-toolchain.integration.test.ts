import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { resolveBundledAuthToolchain } from './bootstrap-toolchain'

function sha256(filePath: string): string {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function asset(root: string, file: string, version?: string) {
  const filePath = path.join(root, file)
  const record = { file, size: fs.statSync(filePath).size, sha256: sha256(filePath) }

  return version ? { ...record, version } : record
}

function makeToolchain() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-toolchain-runtime-'))
  fs.mkdirSync(path.join(root, 'wheelhouse'))
  fs.writeFileSync(path.join(root, 'uv.gz'), 'fixture compressed uv\n')
  fs.writeFileSync(path.join(root, 'python.tar.gz'), 'fixture python\n')
  fs.writeFileSync(path.join(root, 'auth-requirements.txt'), 'fixture requirements\n')
  fs.writeFileSync(path.join(root, 'wheelhouse', 'httpx.whl'), 'fixture wheel\n')
  const manifest = {
    schemaVersion: 1,
    platform: 'darwin',
    arch: 'arm64',
    uv: asset(root, 'uv.gz', '0.12.5'),
    python: asset(root, 'python.tar.gz', '3.11.16'),
    requirements: asset(root, 'auth-requirements.txt'),
    wheels: [asset(root, 'wheelhouse/httpx.whl')]
  }
  fs.writeFileSync(path.join(root, 'manifest.json'), JSON.stringify(manifest))

  return { root, manifest }
}

test('resolveBundledAuthToolchain verifies every packaged asset', async () => {
  const fixture = makeToolchain()

  try {
    const resolved = await resolveBundledAuthToolchain(fixture.root)

    assert.deepEqual(resolved.manifest, fixture.manifest)
    assert.equal(resolved.root, fixture.root)
    assert.equal(resolved.uvArchivePath, path.join(fixture.root, 'uv.gz'))
    assert.equal(resolved.pythonArchivePath, path.join(fixture.root, 'python.tar.gz'))
    assert.equal(resolved.requirementsPath, path.join(fixture.root, 'auth-requirements.txt'))
    assert.deepEqual(resolved.wheelPaths, [path.join(fixture.root, 'wheelhouse', 'httpx.whl')])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('resolveBundledAuthToolchain rejects changed files and links', async () => {
  const tampered = makeToolchain()
  const linked = makeToolchain()

  try {
    fs.appendFileSync(path.join(tampered.root, 'uv.gz'), 'changed\n')
    await assert.rejects(resolveBundledAuthToolchain(tampered.root), /size mismatch|checksum mismatch/)

    const wheelPath = path.join(linked.root, 'wheelhouse', 'httpx.whl')
    fs.unlinkSync(wheelPath)
    fs.symlinkSync(path.join(linked.root, 'uv.gz'), wheelPath)
    await assert.rejects(resolveBundledAuthToolchain(linked.root), /regular non-link file/)
  } finally {
    fs.rmSync(tampered.root, { recursive: true, force: true })
    fs.rmSync(linked.root, { recursive: true, force: true })
  }
})

test('resolveBundledAuthToolchain rejects path traversal and the wrong target', async () => {
  const traversal = makeToolchain()
  const wrongTarget = makeToolchain()

  try {
    const traversalManifest = JSON.parse(
      fs.readFileSync(path.join(traversal.root, 'manifest.json'), 'utf8')
    )
    traversalManifest.wheels[0].file = '../outside.whl'
    fs.writeFileSync(path.join(traversal.root, 'manifest.json'), JSON.stringify(traversalManifest))
    await assert.rejects(resolveBundledAuthToolchain(traversal.root), /wheel .*file path is invalid/)

    const wrongManifest = JSON.parse(
      fs.readFileSync(path.join(wrongTarget.root, 'manifest.json'), 'utf8')
    )
    wrongManifest.arch = 'x64'
    fs.writeFileSync(path.join(wrongTarget.root, 'manifest.json'), JSON.stringify(wrongManifest))
    await assert.rejects(resolveBundledAuthToolchain(wrongTarget.root), /unsupported authentication toolchain target/)
  } finally {
    fs.rmSync(traversal.root, { recursive: true, force: true })
    fs.rmSync(wrongTarget.root, { recursive: true, force: true })
  }
})

test('resolveBundledAuthToolchain accepts the verified Windows x64 asset layout only for Windows x64', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-toolchain-win32-'))

  try {
    fs.mkdirSync(path.join(root, 'wheelhouse'))
    fs.writeFileSync(path.join(root, 'uv.exe'), 'fixture PE32+ x64 uv\n')
    fs.writeFileSync(path.join(root, 'python-embed.zip'), 'fixture CPython embed archive\n')
    fs.writeFileSync(path.join(root, 'auth-requirements.txt'), 'fixture requirements\n')
    fs.writeFileSync(path.join(root, 'wheelhouse', 'keyring-25.7.0-py3-none-any.whl'), 'fixture wheel\n')
    const manifest = {
      schemaVersion: 1,
      platform: 'win32',
      arch: 'x64',
      uv: asset(root, 'uv.exe', '0.12.5'),
      python: asset(root, 'python-embed.zip', '3.13.15'),
      requirements: asset(root, 'auth-requirements.txt'),
      wheels: [asset(root, 'wheelhouse/keyring-25.7.0-py3-none-any.whl')]
    }
    fs.writeFileSync(path.join(root, 'manifest.json'), JSON.stringify(manifest))

    const resolved = await resolveBundledAuthToolchain(root, { platform: 'win32', arch: 'x64' })
    assert.equal(resolved.uvAssetPath, path.join(root, 'uv.exe'))
    assert.equal(resolved.pythonArchivePath, path.join(root, 'python-embed.zip'))
    await assert.rejects(
      resolveBundledAuthToolchain(root, { platform: 'darwin', arch: 'arm64' }),
      /target mismatch/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
