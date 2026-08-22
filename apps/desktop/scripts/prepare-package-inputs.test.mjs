import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import desktopPackage from '../package.json' with { type: 'json' }
import { packageInputPlan, packageResourcePlan, preparePackageInputs } from './prepare-package-inputs.mjs'

const MAC_OUTPUTS = [
  'build/bootstrap/install.sh',
  'build/bootstrap/hermes-backend.tar.gz',
  'build/bootstrap/payload-manifest.json',
  'build/bootstrap/auth-toolchain/manifest.json'
]
const WINDOWS_OUTPUTS = [
  'build/bootstrap/install.ps1',
  'build/bootstrap/hermes-backend.tar.gz',
  'build/bootstrap/payload-manifest.json',
  'build/bootstrap/auth-toolchain/manifest.json',
  'build/windows-prereqs/git-bash-runtime.tar.xz'
]

test('package input plans close the two supported platform resource sets', () => {
  const root = '/repo'
  const desktop = '/repo/apps/desktop'
  assert.deepEqual(packageInputPlan({ platform: 'darwin', arch: 'arm64', repoRoot: root, desktopRoot: desktop }).outputs, MAC_OUTPUTS)
  assert.deepEqual(packageInputPlan({ platform: 'win32', arch: 'x64', repoRoot: root, desktopRoot: desktop }).outputs, WINDOWS_OUTPUTS)
  assert.throws(
    () => packageInputPlan({ platform: 'darwin', arch: 'x64', repoRoot: root, desktopRoot: desktop }),
    /unsupported package target/
  )
})

test('Electron Builder mappings equal the verified package resource plan one-for-one', () => {
  for (const [platform, arch, key] of [
    ['darwin', 'arm64', 'mac'],
    ['win32', 'x64', 'win']
  ]) {
    const expected = packageResourcePlan({ platform, arch })
    assert.deepEqual(desktopPackage.build[key].extraResources, expected)
  }
})

test('release package commands cannot bypass package input preparation', () => {
  assert.equal(desktopPackage.scripts['prepare:package:mac'], 'node scripts/prepare-package-inputs.mjs --platform darwin')
  assert.equal(desktopPackage.scripts['prepare:package:win'], 'node scripts/prepare-package-inputs.mjs --platform win32')
  assert.equal(
    desktopPackage.scripts['dist:mac:dmg'],
    'npm run prepare:package:mac && npm run build && npm run builder -- --mac dmg'
  )
  assert.equal(
    desktopPackage.scripts['dist:win:nsis'],
    'npm run prepare:package:win && npm run build && npm run builder -- --win nsis'
  )
})

test('preparePackageInputs atomically publishes only the declared outputs', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-package-inputs-'))
  const desktopRoot = path.join(root, 'apps', 'desktop')
  fs.mkdirSync(path.join(desktopRoot, 'build'), { recursive: true })
  const commit = 'a'.repeat(40)

  try {
    const result = await preparePackageInputs({
      platform: 'win32',
      arch: 'x64',
      repoRoot: root,
      desktopRoot,
      env: {},
      dependencies: {
        resolveSource: () => ({ commit, branch: 'main', dirty: false }),
        buildBackend: ({ outputDir }) => {
          fs.mkdirSync(outputDir, { recursive: true })
          fs.writeFileSync(path.join(outputDir, 'install.ps1'), 'exit 0')
          fs.writeFileSync(path.join(outputDir, 'hermes-backend.tar.gz'), 'backend')
          fs.writeFileSync(path.join(outputDir, 'payload-manifest.json'), '{}')
        },
        prepareAuth: async ({ outputDir }) => {
          fs.mkdirSync(path.join(outputDir, 'wheelhouse'), { recursive: true })
          fs.writeFileSync(path.join(outputDir, 'uv.exe'), 'uv')
          fs.writeFileSync(path.join(outputDir, 'python-embed.zip'), 'python')
          fs.writeFileSync(path.join(outputDir, 'auth-requirements.txt'), 'requirements')
          fs.writeFileSync(path.join(outputDir, 'wheelhouse', 'keyring-py3-none-any.whl'), 'wheel')
          fs.writeFileSync(path.join(outputDir, 'manifest.json'), JSON.stringify({
            schemaVersion: 1,
            platform: 'win32',
            arch: 'x64',
            uv: { file: 'uv.exe', size: 2, sha256: 'a'.repeat(64), version: '0.12.5' },
            python: { file: 'python-embed.zip', size: 6, sha256: 'b'.repeat(64), version: '3.13.15' },
            requirements: { file: 'auth-requirements.txt', size: 12, sha256: 'c'.repeat(64) },
            wheels: [{ file: 'wheelhouse/keyring-py3-none-any.whl', size: 5, sha256: 'd'.repeat(64) }]
          }))
        },
        prepareWindowsGit: async ({ outputPath }) => {
          fs.mkdirSync(path.dirname(outputPath), { recursive: true })
          fs.writeFileSync(outputPath, 'git runtime')
        },
        verifyAuth: () => true,
        verifyPayload: () => true,
        verifyGit: () => true
      }
    })

    assert.deepEqual(result.outputs, WINDOWS_OUTPUTS)
    for (const relative of WINDOWS_OUTPUTS) {
      assert.ok(fs.statSync(path.join(desktopRoot, relative)).isFile(), relative)
    }
    assert.equal(fs.existsSync(path.join(desktopRoot, 'build', 'bootstrap', 'unexpected.txt')), false)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('preparePackageInputs rejects extra package-root outputs before replacing an existing package', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-package-inputs-extra-'))
  const desktopRoot = path.join(root, 'apps', 'desktop')
  const previousRoot = path.join(desktopRoot, 'build', 'bootstrap')
  fs.mkdirSync(previousRoot, { recursive: true })
  fs.writeFileSync(path.join(previousRoot, 'previous.txt'), 'preserve')

  try {
    await assert.rejects(
      preparePackageInputs({
        platform: 'darwin',
        arch: 'arm64',
        repoRoot: root,
        desktopRoot,
        env: {},
        dependencies: {
          resolveSource: () => ({ commit: 'a'.repeat(40), branch: 'main', dirty: false }),
          buildBackend: ({ outputDir }) => {
            fs.mkdirSync(outputDir, { recursive: true })
            for (const file of ['install.sh', 'hermes-backend.tar.gz', 'payload-manifest.json', 'unexpected.txt']) {
              fs.writeFileSync(path.join(outputDir, file), file)
            }
          },
          prepareAuth: async ({ outputDir }) => {
            fs.mkdirSync(path.join(outputDir, 'wheelhouse'), { recursive: true })
            for (const [file, contents] of [
              ['uv.gz', 'uv'],
              ['python.tar.gz', 'python'],
              ['auth-requirements.txt', 'requirements'],
              ['wheelhouse/keyring-py3-none-any.whl', 'wheel']
            ]) {
              fs.writeFileSync(path.join(outputDir, file), contents)
            }
            fs.writeFileSync(path.join(outputDir, 'manifest.json'), JSON.stringify({
              uv: { file: 'uv.gz' },
              python: { file: 'python.tar.gz' },
              requirements: { file: 'auth-requirements.txt' },
              wheels: [{ file: 'wheelhouse/keyring-py3-none-any.whl' }]
            }))
          },
          verifyAuth: () => true,
          verifyPayload: () => true
        }
      }),
      /unexpected package output/
    )
    assert.equal(fs.readFileSync(path.join(previousRoot, 'previous.txt'), 'utf8'), 'preserve')
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
