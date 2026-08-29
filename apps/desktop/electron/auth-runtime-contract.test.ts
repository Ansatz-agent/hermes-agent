import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { AUTH_BRIDGE_PROTOCOL_VERSION } from './auth-bridge'
import { type AuthRuntimeMarker, probeAuthRuntime, validateAuthRuntimeContract } from './auth-runtime-contract'
import { DESKTOP_SCOPE_PROTOCOL_VERSION } from './auth-scope-token'

const COMMIT = 'a'.repeat(40)
const ARCHIVE_SHA = 'b'.repeat(64)

function sha256(contents: string): string {
  return crypto.createHash('sha256').update(contents).digest('hex')
}

function writeFile(root: string, relative: string, contents = 'fixture\n'): string {
  const target = path.join(root, relative)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, contents)

  return target
}

function createManagedFixture(prefix = 'hermes-auth-contract-') {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  const activeRoot = path.join(tempRoot, 'Hermes 登录', 'hermes-agent')
  const bootstrapRoot = path.join(tempRoot, 'packaged bootstrap')
  const lockContents = 'version = 1\nrevision = 3\n'

  writeFile(activeRoot, 'auth-venv/python.exe')
  writeFile(activeRoot, 'hermes_cli/main.py')
  writeFile(activeRoot, 'hermes_cli/client_auth/bridge.py')
  writeFile(
    activeRoot,
    'hermes_cli/client_auth/backend_scope_protocol.py',
    `DESKTOP_SCOPE_PROTOCOL_VERSION = ${DESKTOP_SCOPE_PROTOCOL_VERSION}\n`
  )
  writeFile(activeRoot, 'hermes_cli/client_auth/cli.py')
  writeFile(activeRoot, 'bin/ansatz.cmd')
  writeFile(activeRoot, 'desktop_auth_runtime/uv.lock', lockContents)
  writeFile(
    activeRoot,
    '.hermes-bundled-source.json',
    JSON.stringify({
      schemaVersion: 1,
      commit: COMMIT,
      archiveSha256: ARCHIVE_SHA,
      installedAt: '2026-08-22T00:00:00.000Z'
    })
  )
  writeFile(
    bootstrapRoot,
    'payload-manifest.json',
    JSON.stringify({
      schemaVersion: 1,
      commit: COMMIT,
      branch: 'integration/desktop-windows-auth-e2e',
      archive: {
        file: 'hermes-backend.tar.gz',
        size: 1,
        sha256: ARCHIVE_SHA
      },
      installer: { file: 'install.ps1', size: 1, sha256: 'c'.repeat(64) }
    })
  )

  const marker: AuthRuntimeMarker = {
    schemaVersion: 2,
    scope: 'auth',
    sourceCommit: COMMIT,
    sourceArchiveSha256: ARCHIVE_SHA,
    authLockSha256: sha256(lockContents),
    protocolVersion: AUTH_BRIDGE_PROTOCOL_VERSION
  }

  writeFile(activeRoot, '.hermes-auth-bootstrap-complete', JSON.stringify(marker))

  return { tempRoot, activeRoot, bootstrapRoot, marker }
}

function validateFixture(fixture: ReturnType<typeof createManagedFixture>, overrides: Record<string, unknown> = {}) {
  return validateAuthRuntimeContract({
    activeRoot: fixture.activeRoot,
    bundledBootstrapRoot: fixture.bootstrapRoot,
    platform: 'win32',
    requireLauncher: true,
    ...overrides
  })
}

test('schema-2 managed auth contract matches current packaged payload and lock', () => {
  const fixture = createManagedFixture()

  try {
    const result = validateFixture(fixture)

    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
    assert.equal(result.pythonPath, path.join(fixture.activeRoot, 'auth-venv', 'python.exe'))
    assert.deepEqual(result.marker, fixture.marker)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('managed auth contract accepts a legacy launcher only as a compatibility fallback', () => {
  const fixture = createManagedFixture()

  try {
    fs.rmSync(path.join(fixture.activeRoot, 'bin', 'ansatz.cmd'))
    writeFile(fixture.activeRoot, 'bin/hermes.cmd')

    assert.equal(validateFixture(fixture).ok, true)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('managed auth contract skips an invalid canonical launcher before legacy fallback', () => {
  const fixture = createManagedFixture()

  try {
    fs.rmSync(path.join(fixture.activeRoot, 'bin', 'ansatz.cmd'))
    fs.mkdirSync(path.join(fixture.activeRoot, 'bin', 'ansatz.cmd'))
    writeFile(fixture.activeRoot, 'bin/hermes.cmd')

    assert.equal(validateFixture(fixture).ok, true)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth contract rejects schema drift, stale package state, and pending transactions', () => {
  const cases: Array<[string, (fixture: ReturnType<typeof createManagedFixture>) => void]> = [
    [
      'schema-1 marker',
      fixture =>
        writeFile(
          fixture.activeRoot,
          '.hermes-auth-bootstrap-complete',
          JSON.stringify({ schemaVersion: 1, scope: 'auth' })
        )
    ],
    [
      'extra marker key',
      fixture =>
        writeFile(
          fixture.activeRoot,
          '.hermes-auth-bootstrap-complete',
          JSON.stringify({ ...fixture.marker, completedAt: 'not-in-contract' })
        )
    ],
    [
      'wrong current package commit',
      fixture =>
        writeFile(
          fixture.bootstrapRoot,
          'payload-manifest.json',
          JSON.stringify({
            schemaVersion: 1,
            commit: 'd'.repeat(40),
            archive: { file: 'hermes-backend.tar.gz', size: 1, sha256: ARCHIVE_SHA },
            installer: { file: 'install.ps1', size: 1, sha256: 'c'.repeat(64) }
          })
        )
    ],
    [
      'wrong active source archive',
      fixture =>
        writeFile(
          fixture.activeRoot,
          '.hermes-bundled-source.json',
          JSON.stringify({
            schemaVersion: 1,
            commit: COMMIT,
            archiveSha256: 'e'.repeat(64),
            installedAt: '2026-08-22T00:00:00.000Z'
          })
        )
    ],
    ['wrong lock hash', fixture => writeFile(fixture.activeRoot, 'desktop_auth_runtime/uv.lock', 'changed\n')],
    [
      'wrong protocol',
      fixture =>
        writeFile(
          fixture.activeRoot,
          '.hermes-auth-bootstrap-complete',
          JSON.stringify({ ...fixture.marker, protocolVersion: 99 })
        )
    ],
    ['pending transaction', fixture => writeFile(fixture.activeRoot, 'auth-venv.pending-backup', '{}')]
  ]

  for (const [label, mutate] of cases) {
    const fixture = createManagedFixture()

    try {
      mutate(fixture)
      assert.equal(validateFixture(fixture).ok, false, label)
    } finally {
      fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
    }
  }
})

test('auth contract reports a desktop scope protocol mismatch before runtime startup', () => {
  const fixture = createManagedFixture()

  try {
    writeFile(
      fixture.activeRoot,
      'hermes_cli/client_auth/backend_scope_protocol.py',
      'DESKTOP_SCOPE_PROTOCOL_VERSION = 1\n'
    )

    const result = validateFixture(fixture)
    assert.equal(result.ok, false)
    assert.equal(result.reason, 'scope_protocol_mismatch')
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth contract accepts an annotated desktop scope protocol declaration', () => {
  const fixture = createManagedFixture()

  try {
    writeFile(
      fixture.activeRoot,
      'hermes_cli/client_auth/backend_scope_protocol.py',
      `from typing import Final\nDESKTOP_SCOPE_PROTOCOL_VERSION: Final[int] = ${DESKTOP_SCOPE_PROTOCOL_VERSION}  # wire contract\n`
    )

    const result = validateFixture(fixture)
    assert.equal(result.ok, true)
    assert.equal(result.reason, null)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth contract rejects symlinked required artifacts', () => {
  const fixture = createManagedFixture()

  try {
    const bridge = path.join(fixture.activeRoot, 'hermes_cli', 'client_auth', 'bridge.py')
    const target = path.join(fixture.tempRoot, 'outside-bridge.py')
    fs.writeFileSync(target, 'fixture\n')
    fs.rmSync(bridge)
    fs.symlinkSync(target, bridge)

    assert.equal(validateFixture(fixture).ok, false)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth contract rejects a symlinked completion marker', () => {
  const fixture = createManagedFixture()

  try {
    const markerPath = path.join(fixture.activeRoot, '.hermes-auth-bootstrap-complete')
    const target = path.join(fixture.tempRoot, 'outside-auth-marker.json')
    fs.writeFileSync(target, JSON.stringify(fixture.marker))
    fs.rmSync(markerPath)
    fs.symlinkSync(target, markerPath)

    assert.equal(validateFixture(fixture).ok, false)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('Git auth contract uses current HEAD and a null archive hash', () => {
  const fixture = createManagedFixture('LOCALAPPDATA Hermes auth contract ')

  try {
    fs.rmSync(path.join(fixture.activeRoot, '.hermes-bundled-source.json'))
    fs.mkdirSync(path.join(fixture.activeRoot, '.git'))
    const gitMarker = { ...fixture.marker, sourceArchiveSha256: null }
    writeFile(fixture.activeRoot, '.hermes-auth-bootstrap-complete', JSON.stringify(gitMarker))

    const result = validateAuthRuntimeContract({
      activeRoot: fixture.activeRoot,
      bundledBootstrapRoot: null,
      platform: 'win32',
      requireLauncher: true,
      resolveGitHead: () => COMMIT
    })

    assert.equal(result.ok, true)
    assert.deepEqual(result.marker, gitMarker)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('packaged auth contract cannot be downgraded to Git mode by a stray .git path', () => {
  const fixture = createManagedFixture()

  try {
    fs.mkdirSync(path.join(fixture.activeRoot, '.git'))
    writeFile(
      fixture.activeRoot,
      '.hermes-auth-bootstrap-complete',
      JSON.stringify({ ...fixture.marker, sourceArchiveSha256: null })
    )

    const result = validateAuthRuntimeContract({
      activeRoot: fixture.activeRoot,
      bundledBootstrapRoot: fixture.bootstrapRoot,
      platform: 'win32',
      requireLauncher: true,
      resolveGitHead: () => COMMIT
    })

    assert.equal(result.ok, false)
    assert.equal(result.reason, 'bundled_source_mismatch')
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth runtime probe uses the fixed protocol snippet and sanitized source path', () => {
  const fixture = createManagedFixture()
  const calls: Array<{ command: string; args: string[]; options: Record<string, unknown> }> = []

  try {
    const pythonPath = path.join(fixture.activeRoot, 'auth-venv', 'python.exe')

    const ok = probeAuthRuntime({
      activeRoot: fixture.activeRoot,
      pythonPath,
      runProbe: (command, args, options) => {
        calls.push({ command, args, options })
      }
    })

    assert.equal(ok, true)
    assert.equal(calls.length, 1)
    assert.equal(calls[0].command, pythonPath)
    assert.deepEqual(calls[0].args.slice(0, 1), ['-c'])
    assert.match(calls[0].args[1], /hermes_cli\.client_auth\.bridge/)
    assert.match(calls[0].args[1], new RegExp(`PROTOCOL_VERSION == ${AUTH_BRIDGE_PROTOCOL_VERSION}`))
    assert.match(calls[0].args[1], /hermes_cli\.client_auth\.backend_scope_protocol/)
    assert.match(calls[0].args[1], new RegExp(`DESKTOP_SCOPE_PROTOCOL_VERSION == ${DESKTOP_SCOPE_PROTOCOL_VERSION}`))
    assert.equal((calls[0].options.env as NodeJS.ProcessEnv).PYTHONPATH, fixture.activeRoot)
    assert.equal('OPENAI_API_KEY' in (calls[0].options.env as NodeJS.ProcessEnv), false)
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})

test('auth runtime probe fails closed on execution error', () => {
  const fixture = createManagedFixture()

  try {
    assert.equal(
      probeAuthRuntime({
        activeRoot: fixture.activeRoot,
        pythonPath: path.join(fixture.activeRoot, 'auth-venv', 'python.exe'),
        runProbe: () => {
          throw new Error('protocol mismatch')
        }
      }),
      false
    )
  } finally {
    fs.rmSync(fixture.tempRoot, { recursive: true, force: true })
  }
})
