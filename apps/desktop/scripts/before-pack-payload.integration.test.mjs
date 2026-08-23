import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { verifyPreparedPackageInputs } from './before-pack.mjs'

const PLATFORM_OUTPUTS = {
  darwin: [
    'build/bootstrap/install.sh',
    'build/bootstrap/hermes-backend.tar.gz',
    'build/bootstrap/payload-manifest.json',
    'build/bootstrap/auth-toolchain/manifest.json'
  ],
  win32: [
    'build/bootstrap/install.ps1',
    'build/bootstrap/hermes-backend.tar.gz',
    'build/bootstrap/payload-manifest.json',
    'build/bootstrap/auth-toolchain/manifest.json',
    'build/windows-prereqs/git-bash-runtime.tar.xz'
  ]
}

function writePreparedOutputs(desktopRoot, outputs) {
  for (const relativePath of outputs) {
    const outputPath = path.join(desktopRoot, relativePath)
    fs.mkdirSync(path.dirname(outputPath), { recursive: true })
    fs.writeFileSync(outputPath, `fixture for ${relativePath}`)
  }
}

test('prepared payload validation is skipped outside packaged Desktop platforms', () => {
  assert.equal(verifyPreparedPackageInputs('linux', 'x64', '/unused'), false)
})

for (const [platform, arch] of [['darwin', 'arm64'], ['win32', 'x64']]) {
  test(`beforePack accepts the complete prepared ${platform} payload closure`, () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-payload-'))
    const desktopRoot = path.join(root, 'apps', 'desktop')

    try {
      writePreparedOutputs(desktopRoot, PLATFORM_OUTPUTS[platform])
      assert.equal(verifyPreparedPackageInputs(platform, arch, desktopRoot), true)
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })
}

test('beforePack rejects an empty prepared payload file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-before-pack-payload-'))
  const desktopRoot = path.join(root, 'apps', 'desktop')

  try {
    writePreparedOutputs(desktopRoot, PLATFORM_OUTPUTS.darwin)
    fs.writeFileSync(path.join(desktopRoot, 'build/bootstrap/hermes-backend.tar.gz'), '')
    assert.throws(
      () => verifyPreparedPackageInputs('darwin', 'arm64', desktopRoot),
      /prepared package input is invalid/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
