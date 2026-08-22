import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { gunzipSync } from 'node:zlib'

import { test } from 'vitest'

import {
  AUTH_TOOLCHAIN_ARCH,
  AUTH_TOOLCHAIN_PLATFORM,
  buildAuthToolchain,
  buildAuthToolchainFromEnvironment,
  verifyAuthToolchain
} from './build-auth-toolchain.mjs'

function sha256(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-toolchain-'))
  const inputs = path.join(root, 'inputs')
  const wheelhousePath = path.join(inputs, 'wheelhouse')
  const outputDir = path.join(root, 'output')
  const uvPath = path.join(inputs, 'uv')
  const pythonArchivePath = path.join(inputs, 'python.tar.gz')
  const requirementsPath = path.join(inputs, 'auth-requirements.txt')

  fs.mkdirSync(wheelhousePath, { recursive: true })
  fs.writeFileSync(uvPath, '#!/bin/sh\nprintf "uv 0.12.5\\n"\n')
  fs.chmodSync(uvPath, 0o755)
  fs.writeFileSync(pythonArchivePath, 'fixture relocatable python archive\n')
  fs.writeFileSync(
    requirementsPath,
    'httpx==0.28.1 --hash=sha256:' + 'a'.repeat(64) + '\n'
  )
  fs.writeFileSync(path.join(wheelhousePath, 'httpx-0.28.1-py3-none-any.whl'), 'fixture wheel\n')

  return { root, outputDir, uvPath, pythonArchivePath, requirementsPath, wheelhousePath }
}

function buildOptions(fixture, overrides = {}) {
  return {
    outputDir: fixture.outputDir,
    uvPath: fixture.uvPath,
    pythonArchivePath: fixture.pythonArchivePath,
    requirementsPath: fixture.requirementsPath,
    wheelhousePath: fixture.wheelhousePath,
    platform: AUTH_TOOLCHAIN_PLATFORM,
    arch: AUTH_TOOLCHAIN_ARCH,
    uvVersion: '0.12.5',
    pythonVersion: '3.11.14',
    ...overrides
  }
}

test('buildAuthToolchain writes and verifies a deterministic manifest', () => {
  const fixture = makeFixture()

  try {
    const result = buildAuthToolchain(buildOptions(fixture))
    const manifestPath = path.join(fixture.outputDir, 'manifest.json')
    const uvOutput = path.join(fixture.outputDir, result.manifest.uv.file)
    const pythonOutput = path.join(fixture.outputDir, result.manifest.python.file)
    const requirementsOutput = path.join(fixture.outputDir, result.manifest.requirements.file)
    const wheelOutput = path.join(fixture.outputDir, result.manifest.wheels[0].file)

    assert.equal(result.manifest.schemaVersion, 1)
    assert.equal(result.manifest.platform, 'darwin')
    assert.equal(result.manifest.arch, 'arm64')
    assert.equal(result.manifest.uv.file, 'uv.gz')
    assert.equal(result.manifest.uv.version, '0.12.5')
    assert.equal(result.manifest.python.version, '3.11.14')
    assert.equal(result.manifest.uv.sha256, sha256(uvOutput))
    assert.equal(result.manifest.python.sha256, sha256(pythonOutput))
    assert.equal(result.manifest.requirements.sha256, sha256(requirementsOutput))
    assert.equal(result.manifest.wheels[0].sha256, sha256(wheelOutput))
    assert.deepEqual(verifyAuthToolchain(fixture.outputDir), result.manifest)
    assert.deepEqual(gunzipSync(fs.readFileSync(uvOutput)), fs.readFileSync(fixture.uvPath))
    assert.equal(fs.existsSync(path.join(fixture.outputDir, 'uv')), false)
    assert.ok(fs.statSync(manifestPath).isFile())
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('verifyAuthToolchain rejects a changed asset', () => {
  const fixture = makeFixture()

  try {
    const result = buildAuthToolchain(buildOptions(fixture))
    fs.appendFileSync(path.join(fixture.outputDir, result.manifest.uv.file), 'tampered\n')

    assert.throws(() => verifyAuthToolchain(fixture.outputDir), /size mismatch|checksum mismatch/)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('buildAuthToolchain produces the same uv archive across output directories', () => {
  const fixture = makeFixture()
  const secondOutputDir = path.join(fixture.root, 'second-output')

  try {
    const first = buildAuthToolchain(buildOptions(fixture))
    const second = buildAuthToolchain(buildOptions(fixture, { outputDir: secondOutputDir }))

    assert.deepEqual(first.manifest, second.manifest)
    assert.deepEqual(
      fs.readFileSync(path.join(fixture.outputDir, first.manifest.uv.file)),
      fs.readFileSync(path.join(secondOutputDir, second.manifest.uv.file))
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('buildAuthToolchain rejects links and the wrong target', () => {
  const fixture = makeFixture()

  try {
    const linkedWheel = path.join(fixture.wheelhousePath, 'linked.whl')
    fs.symlinkSync(path.join(fixture.wheelhousePath, 'httpx-0.28.1-py3-none-any.whl'), linkedWheel)

    assert.throws(() => buildAuthToolchain(buildOptions(fixture)), /regular non-link file/)
    fs.unlinkSync(linkedWheel)
    assert.throws(
      () => buildAuthToolchain(buildOptions(fixture, { arch: 'ia32' })),
      /unsupported authentication toolchain target/
    )
    assert.throws(
      () => buildAuthToolchain(buildOptions(fixture, { platform: 'linux' })),
      /unsupported authentication toolchain target/
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('buildAuthToolchain writes a Windows x64 layout without a runtime download', () => {
  const fixture = makeFixture()
  const windowsUv = path.join(path.dirname(fixture.uvPath), 'uv.exe')
  const windowsPython = path.join(path.dirname(fixture.pythonArchivePath), 'python-embed.zip')

  try {
    fs.writeFileSync(windowsUv, 'fixture win32 x64 uv executable\n')
    fs.writeFileSync(windowsPython, 'fixture CPython 3.13.15 embed archive\n')
    const result = buildAuthToolchain(
      buildOptions(fixture, {
        platform: 'win32',
        arch: 'x64',
        uvPath: windowsUv,
        pythonArchivePath: windowsPython,
        pythonVersion: '3.13.15'
      })
    )

    assert.equal(result.manifest.platform, 'win32')
    assert.equal(result.manifest.arch, 'x64')
    assert.equal(result.manifest.uv.file, 'uv.exe')
    assert.equal(result.manifest.python.file, 'python-embed.zip')
    assert.equal(result.manifest.uv.version, '0.12.5')
    assert.equal(result.manifest.python.version, '3.13.15')
    assert.deepEqual(fs.readFileSync(path.join(fixture.outputDir, 'uv.exe')), fs.readFileSync(windowsUv))
    assert.deepEqual(verifyAuthToolchain(fixture.outputDir), result.manifest)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('Windows toolchain rejects a non-Windows wheel before publishing', () => {
  const fixture = makeFixture()

  try {
    fs.rmSync(fixture.wheelhousePath, { recursive: true, force: true })
    fs.mkdirSync(fixture.wheelhousePath)
    fs.writeFileSync(
      path.join(fixture.wheelhousePath, 'keyring-25.7.0-cp313-cp313-macosx_11_0_arm64.whl'),
      'wrong platform\n'
    )

    assert.throws(
      () =>
        buildAuthToolchain(
          buildOptions(fixture, {
            platform: 'win32',
            arch: 'x64',
            pythonVersion: '3.13.15'
          })
        ),
      /not Windows x64 compatible/
    )
    assert.equal(fs.existsSync(fixture.outputDir), false)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('buildAuthToolchainFromEnvironment requires explicit release inputs', () => {
  const fixture = makeFixture()

  try {
    assert.throws(() => buildAuthToolchainFromEnvironment({}), /HERMES_AUTH_TOOLCHAIN_OUTPUT_DIR/)

    const result = buildAuthToolchainFromEnvironment({
      HERMES_AUTH_TOOLCHAIN_OUTPUT_DIR: fixture.outputDir,
      HERMES_AUTH_TOOLCHAIN_UV_PATH: fixture.uvPath,
      HERMES_AUTH_TOOLCHAIN_PYTHON_ARCHIVE: fixture.pythonArchivePath,
      HERMES_AUTH_TOOLCHAIN_REQUIREMENTS: fixture.requirementsPath,
      HERMES_AUTH_TOOLCHAIN_WHEELHOUSE: fixture.wheelhousePath,
      HERMES_AUTH_TOOLCHAIN_UV_VERSION: '0.12.5',
      HERMES_AUTH_TOOLCHAIN_PYTHON_VERSION: '3.11.14'
    })

    assert.deepEqual(verifyAuthToolchain(fixture.outputDir), result.manifest)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})
