import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildBackendPayload,
  RUNTIME_SCRIPT_FILES,
  validateInstallStamp
} from './build-backend-payload.mjs'

function git(repoRoot, ...args) {
  return execFileSync('git', args, { cwd: repoRoot, encoding: 'utf8' }).trim()
}

function makeRepository(extraFiles = {}) {
  const repoRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-backend-payload-repo-'))
  fs.mkdirSync(path.join(repoRoot, 'scripts'), { recursive: true })
  fs.writeFileSync(path.join(repoRoot, 'payload.txt'), 'committed payload\n')
  fs.writeFileSync(path.join(repoRoot, 'scripts', 'install.sh'), '#!/bin/sh\nexit 0\n')
  fs.writeFileSync(path.join(repoRoot, 'scripts', 'install.ps1'), 'exit 0\r\n')
  for (const [relativePath, contents] of Object.entries(extraFiles)) {
    const target = path.join(repoRoot, relativePath)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, contents)
  }
  git(repoRoot, 'init')
  git(repoRoot, 'config', 'user.name', 'Hermes Test')
  git(repoRoot, 'config', 'user.email', 'hermes-test@example.invalid')
  git(repoRoot, 'add', '.')
  git(repoRoot, 'commit', '-m', 'fixture')

  const commit = git(repoRoot, 'rev-parse', 'HEAD')
  const stampPath = path.join(repoRoot, 'install-stamp.json')
  const outputDir = path.join(repoRoot, 'output')
  fs.writeFileSync(
    stampPath,
    JSON.stringify({ schemaVersion: 1, commit, branch: 'fixture', dirty: false })
  )

  return { repoRoot, commit, stampPath, outputDir }
}

function sha256(filePath) {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

test('backend payload is generated only from the committed tree with matching checksums', () => {
  const fixture = makeRepository()
  try {
    fs.writeFileSync(path.join(fixture.repoRoot, 'untracked-secret.txt'), 'must not ship\n')
    const result = buildBackendPayload({
      repoRoot: fixture.repoRoot,
      stampPath: fixture.stampPath,
      outputDir: fixture.outputDir,
      payloadPaths: ['payload.txt'],
      payloadExcludes: [],
      requiredEntries: ['hermes-agent/payload.txt']
    })

    const archivePath = path.join(fixture.outputDir, result.manifest.archive.file)
    const installerPath = path.join(fixture.outputDir, result.manifest.installer.file)
    assert.equal(result.manifest.commit, fixture.commit)
    assert.equal(result.manifest.archive.sha256, sha256(archivePath))
    assert.equal(result.manifest.installer.sha256, sha256(installerPath))
    assert.deepEqual(result.entries, ['hermes-agent/', 'hermes-agent/payload.txt'])
    assert.equal(result.entries.some(entry => entry.includes('untracked-secret')), false)
  } finally {
    fs.rmSync(fixture.repoRoot, { recursive: true, force: true })
  }
})

test('Windows backend payload ships the committed PowerShell installer', () => {
  const fixture = makeRepository()
  try {
    const gitRuntimePath = path.join(fixture.repoRoot, 'git-bash-runtime.tar.xz')
    const gitRuntimeProvenancePath = path.join(fixture.repoRoot, 'git-bash-runtime.provenance.json')
    fs.writeFileSync(gitRuntimePath, 'fixture Git Bash runtime')
    fs.writeFileSync(gitRuntimeProvenancePath, JSON.stringify({
      schemaVersion: 1,
      source: {
        file: 'PortableGit-2.55.0.3-64-bit.7z.exe',
        sha256: 'ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d'
      },
      runtime: {
        file: 'git-bash-runtime.tar.xz',
        size: fs.statSync(gitRuntimePath).size,
        sha256: sha256(gitRuntimePath),
        entries: 1
      }
    }))
    const result = buildBackendPayload({
      repoRoot: fixture.repoRoot,
      stampPath: fixture.stampPath,
      outputDir: fixture.outputDir,
      payloadPaths: ['payload.txt'],
      payloadExcludes: [],
      requiredEntries: ['hermes-agent/payload.txt'],
      platform: 'win32',
      gitRuntimePath,
      gitRuntimeProvenancePath,
      gitRuntimeAudit: () => ['cmd/git.exe']
    })

    assert.equal(result.manifest.installer.file, 'install.ps1')
    assert.equal(fs.readFileSync(path.join(fixture.outputDir, 'install.ps1'), 'utf8'), 'exit 0\r\n')
    assert.equal(fs.existsSync(path.join(fixture.outputDir, 'install.sh')), false)
    assert.equal(result.manifest.gitBashRuntime.sha256, sha256(gitRuntimePath))
  } finally {
    fs.rmSync(fixture.repoRoot, { recursive: true, force: true })
  }
})

test('backend payload build rejects tracked working-tree changes', () => {
  const fixture = makeRepository()
  try {
    fs.writeFileSync(path.join(fixture.repoRoot, 'payload.txt'), 'dirty payload\n')
    assert.throws(
      () => buildBackendPayload({
        repoRoot: fixture.repoRoot,
        stampPath: fixture.stampPath,
        outputDir: fixture.outputDir,
        payloadPaths: ['payload.txt'],
        payloadExcludes: [],
        requiredEntries: ['hermes-agent/payload.txt']
      }),
      /tracked working tree is dirty/
    )
  } finally {
    fs.rmSync(fixture.repoRoot, { recursive: true, force: true })
  }
})

test('install stamp validation requires a real clean commit', () => {
  assert.throws(
    () => validateInstallStamp({ schemaVersion: 1, commit: '0'.repeat(40), branch: null, dirty: false }),
    /real 40-character Git commit/
  )
  assert.throws(
    () => validateInstallStamp({ schemaVersion: 1, commit: 'a'.repeat(40), branch: null, dirty: true }),
    /install stamp is dirty/
  )
})

test('macOS backend payload excludes the Android-only psutil installer', () => {
  assert.equal(RUNTIME_SCRIPT_FILES.includes('install_psutil_android.py'), false)
})

test('backend payload rejects CI-only files even under an allowed runtime path', () => {
  const fixture = makeRepository({
    'apps/shared/runtime.txt': 'runtime payload\n',
    'apps/shared/.github/workflows/remote-login.yml': 'name: source-only login check\n'
  })
  try {
    assert.throws(
      () => buildBackendPayload({
        repoRoot: fixture.repoRoot,
        stampPath: fixture.stampPath,
        outputDir: fixture.outputDir,
        payloadPaths: ['apps/shared'],
        payloadExcludes: [],
        requiredEntries: ['hermes-agent/apps/shared/runtime.txt']
      }),
      /CI-only entry/
    )
  } finally {
    fs.rmSync(fixture.repoRoot, { recursive: true, force: true })
  }
})

test('backend payload rejects a credential-login driver nested under an allowed runtime path', () => {
  const fixture = makeRepository({
    'apps/shared/runtime.txt': 'runtime payload\n',
    'apps/shared/desktop-dmg-credential-login.mjs': 'throw new Error("CI only")\n'
  })
  try {
    assert.throws(
      () =>
        buildBackendPayload({
          repoRoot: fixture.repoRoot,
          stampPath: fixture.stampPath,
          outputDir: fixture.outputDir,
          payloadPaths: ['apps/shared'],
          payloadExcludes: [],
          requiredEntries: ['hermes-agent/apps/shared/runtime.txt']
        }),
      /CI-only entry/
    )
  } finally {
    fs.rmSync(fixture.repoRoot, { recursive: true, force: true })
  }
})
