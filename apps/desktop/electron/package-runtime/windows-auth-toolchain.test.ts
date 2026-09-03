import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import { AUTH_BRIDGE_PROTOCOL_VERSION } from '../auth-bridge'
import { DESKTOP_SCOPE_PROTOCOL_VERSION } from '../auth-scope-token'
import type { BundledAuthToolchain } from '../bootstrap-toolchain'

import {
  prepareWindowsPackagedAuthRuntime,
  recoverWindowsAuthRuntimeTransaction,
  windowsPowerShellExecutable
} from './windows-auth-toolchain'

const SOURCE_COMMIT = 'a'.repeat(40)
const SOURCE_ARCHIVE_SHA256 = 'b'.repeat(64)

const EXPAND_ARCHIVE_COMMAND =
  'Expand-Archive -LiteralPath $env:HERMES_ARCHIVE_PATH -DestinationPath $env:HERMES_ARCHIVE_DESTINATION -Force'

function sha256(filePath: string): string {
  return createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

function writePeX64(filePath: string): void {
  const bytes = Buffer.alloc(512)
  bytes.write('MZ', 0, 'ascii')
  bytes.writeUInt32LE(0x80, 0x3c)
  bytes.write('PE\0\0', 0x80, 'binary')
  bytes.writeUInt16LE(0x8664, 0x84)
  fs.writeFileSync(filePath, bytes)
}

function asset(root: string, file: string): { file: string; size: number; sha256: string } {
  const filePath = path.join(root, file)
  const value = { file, size: fs.statSync(filePath).size, sha256: sha256(filePath) }

  return value
}

function versionedAsset(root: string, file: string, version: string) {
  return { ...asset(root, file), version }
}

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-windows-auth-runtime-'))
  const toolchainRoot = path.join(root, 'toolchain')
  const activeRoot = path.join(root, 'hermes-agent')
  fs.mkdirSync(path.join(toolchainRoot, 'wheelhouse'), { recursive: true })
  fs.mkdirSync(path.join(activeRoot, 'hermes_cli', 'client_auth'), { recursive: true })
  fs.mkdirSync(path.join(activeRoot, 'desktop_auth_runtime'), { recursive: true })
  fs.writeFileSync(path.join(activeRoot, 'hermes_cli', 'main.py'), 'fixture\n')
  fs.writeFileSync(path.join(activeRoot, 'hermes_cli', 'client_auth', 'bridge.py'), 'fixture\n')
  fs.writeFileSync(
    path.join(activeRoot, 'hermes_cli', 'client_auth', 'backend_scope_protocol.py'),
    'DESKTOP_SCOPE_PROTOCOL_VERSION = 2\n'
  )
  fs.writeFileSync(path.join(activeRoot, 'hermes_cli', 'client_auth', 'cli.py'), 'fixture\n')
  fs.writeFileSync(path.join(activeRoot, 'desktop_auth_runtime', 'uv.lock'), 'version = 1\nrevision = 3\n')
  fs.writeFileSync(
    path.join(activeRoot, '.hermes-bundled-source.json'),
    JSON.stringify({ schemaVersion: 1, commit: SOURCE_COMMIT, archiveSha256: SOURCE_ARCHIVE_SHA256 })
  )
  writePeX64(path.join(toolchainRoot, 'uv.exe'))
  fs.writeFileSync(path.join(toolchainRoot, 'python-embed.zip'), 'fixture zip')
  fs.writeFileSync(
    path.join(toolchainRoot, 'auth-requirements.txt'),
    `keyring==25.7.0 --hash=sha256:${'a'.repeat(64)}\n`
  )
  fs.writeFileSync(path.join(toolchainRoot, 'wheelhouse', 'keyring-25.7.0-py3-none-any.whl'), 'wheel')

  const manifest = {
    schemaVersion: 1 as const,
    platform: 'win32' as const,
    arch: 'x64' as const,
    uv: versionedAsset(toolchainRoot, 'uv.exe', '0.12.5'),
    python: versionedAsset(toolchainRoot, 'python-embed.zip', '3.13.15'),
    requirements: asset(toolchainRoot, 'auth-requirements.txt'),
    wheels: [asset(toolchainRoot, 'wheelhouse/keyring-25.7.0-py3-none-any.whl')]
  }

  const toolchain: BundledAuthToolchain = {
    root: toolchainRoot,
    manifest,
    manifestPath: path.join(toolchainRoot, 'manifest.json'),
    uvAssetPath: path.join(toolchainRoot, 'uv.exe'),
    uvArchivePath: path.join(toolchainRoot, 'uv.exe'),
    pythonArchivePath: path.join(toolchainRoot, 'python-embed.zip'),
    requirementsPath: path.join(toolchainRoot, 'auth-requirements.txt'),
    wheelPaths: [path.join(toolchainRoot, 'wheelhouse', 'keyring-25.7.0-py3-none-any.whl')]
  }

  return { root, toolchain, activeRoot }
}

test('Windows auth runtime uses System32 PowerShell and only bundled local packages', async () => {
  const fixture = makeFixture()
  const calls: any[] = []
  const retiredRoots: string[] = []
  let verificationPathContents = ''

  try {
    const result = await prepareWindowsPackagedAuthRuntime({
      toolchain: fixture.toolchain,
      activeRoot: fixture.activeRoot,
      env: { SystemRoot: 'C:\\Windows', LOCALAPPDATA: 'C:\\Users\\tester\\AppData\\Local' },
      retireAuthOwners: async ({ activeRoot }) => {
        retiredRoots.push(activeRoot)

        return { inspected: 0, stopped: 0 }
      },
      runProcess: async options => {
        calls.push(options)

        if (options.args.includes(EXPAND_ARCHIVE_COMMAND)) {
          const destination = options.env?.HERMES_ARCHIVE_DESTINATION
          fs.mkdirSync(destination, { recursive: true })
          writePeX64(path.join(destination, 'python.exe'))
          fs.writeFileSync(path.join(destination, 'python313.zip'), 'stdlib')
        }

        if (options.command.endsWith('python.exe')) {
          verificationPathContents = fs.readFileSync(path.join(path.dirname(options.command), 'python313._pth'), 'utf8')
        }

        return { code: 0, killed: false, signal: null, stderr: '', stdout: '', termination: null }
      }
    })

    assert.equal(result.runtimeRoot, path.join(fixture.activeRoot, 'auth-venv'))
    assert.deepEqual(retiredRoots, [fixture.activeRoot])
    assert.equal(
      windowsPowerShellExecutable({ SystemRoot: 'C:\\Windows' }),
      'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'
    )
    const expand = calls[0]
    assert.equal(expand.command, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe')
    assert.ok(expand.args.includes('-NoProfile'))
    assert.ok(expand.args.includes('-NonInteractive'))
    assert.equal(expand.args.includes(fixture.toolchain.pythonArchivePath), false)
    assert.equal(expand.env.HERMES_ARCHIVE_PATH, fixture.toolchain.pythonArchivePath)
    assert.equal(expand.env.HERMES_ARCHIVE_DESTINATION?.includes('.auth-runtime-stage-'), true)
    assert.ok(expand.hardTimeoutMs > 0)
    assert.ok(expand.idleTimeoutMs > 0)

    const install = calls.find(call => call.args[0] === 'pip')
    assert.ok(install)
    assert.ok(install.args.includes('--no-index'))
    assert.ok(install.args.includes('--require-hashes'))
    assert.equal(
      install.args.some(arg => /^https?:/i.test(arg)),
      false
    )
    const verification = calls.find(call => call.command.endsWith('python.exe'))
    assert.ok(verification)
    assert.match(
      verification.args.join(' '),
      /ntsecuritycon, pywintypes, win32api, win32con, win32file, win32pipe, win32security/
    )
    assert.match(verification.args.join(' '), /bridge\.PROTOCOL_VERSION == 2/)
    assert.match(verification.args.join(' '), /DESKTOP_SCOPE_PROTOCOL_VERSION == 2/)
    assert.match(verification.args.join(' '), /endpoint = runtime_endpoint\(\)/)
    assert.match(
      fs.readFileSync(path.join(result.runtimeRoot, 'python313._pth'), 'utf8'),
      /^python313\.zip\n\.\nLib\\site-packages\n\.\.\nimport site\n$/
    )
    assert.match(verificationPathContents, /^python313\.zip\n\.\nLib\\site-packages\n\.\.\\\.\.\nimport site\n$/)
    assert.ok(fs.statSync(path.join(result.runtimeRoot, 'python.exe')).isFile())
    assert.ok(fs.statSync(result.managedUvPath).isFile())
    assert.ok(fs.statSync(path.join(fixture.activeRoot, 'bin', 'ansatz.cmd')).isFile())
    assert.ok(fs.statSync(path.join(fixture.activeRoot, 'bin', 'hermes.cmd')).isFile())
    const marker = JSON.parse(fs.readFileSync(path.join(fixture.activeRoot, '.hermes-auth-bootstrap-complete'), 'utf8'))
    assert.deepEqual(marker, {
      schemaVersion: 2,
      scope: 'auth',
      sourceCommit: SOURCE_COMMIT,
      sourceArchiveSha256: SOURCE_ARCHIVE_SHA256,
      authLockSha256: sha256(path.join(fixture.activeRoot, 'desktop_auth_runtime', 'uv.lock')),
      protocolVersion: 2
    })
    assert.equal(fs.existsSync(path.join(fixture.activeRoot, 'auth-venv.pending-backup')), false)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('Windows auth runtime never publishes a partial extraction', async () => {
  const fixture = makeFixture()

  try {
    await assert.rejects(
      prepareWindowsPackagedAuthRuntime({
        toolchain: fixture.toolchain,
        activeRoot: fixture.activeRoot,
        env: { SystemRoot: 'C:\\Windows' },
        retireAuthOwners: async () => ({ inspected: 0, stopped: 0 }),
        runProcess: async options => {
          if (options.args.includes(EXPAND_ARCHIVE_COMMAND)) {
            const destination = options.env?.HERMES_ARCHIVE_DESTINATION
            fs.mkdirSync(destination, { recursive: true })
            fs.writeFileSync(path.join(destination, 'python.exe'), 'wrong architecture')
          }

          return { code: 0, killed: false, signal: null, stderr: '', stdout: '', termination: null }
        }
      }),
      /Python executable is not PE32\+ x64/
    )
    assert.equal(fs.existsSync(path.join(fixture.activeRoot, 'auth-venv')), false)
    assert.equal(
      fs.readdirSync(fixture.activeRoot).some(name => name.startsWith('.auth-runtime-stage-')),
      false
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('Windows auth runtime restores the previous published runtime when verification fails', async () => {
  const fixture = makeFixture()
  const existing = path.join(fixture.activeRoot, 'auth-venv')
  fs.mkdirSync(existing, { recursive: true })
  fs.writeFileSync(path.join(existing, 'previous.txt'), 'keep me')

  try {
    await assert.rejects(
      prepareWindowsPackagedAuthRuntime({
        toolchain: fixture.toolchain,
        activeRoot: fixture.activeRoot,
        env: { SystemRoot: 'C:\\Windows' },
        retireAuthOwners: async () => ({ inspected: 0, stopped: 0 }),
        runProcess: async options => {
          if (options.args.includes(EXPAND_ARCHIVE_COMMAND)) {
            const destination = options.env?.HERMES_ARCHIVE_DESTINATION
            fs.mkdirSync(destination, { recursive: true })
            writePeX64(path.join(destination, 'python.exe'))
            fs.writeFileSync(path.join(destination, 'python313.zip'), 'stdlib')
          }

          if (options.command.endsWith('python.exe')) {
            return {
              code: 1,
              killed: false,
              signal: null,
              stderr: 'verification failed',
              stdout: '',
              termination: null
            }
          }

          return { code: 0, killed: false, signal: null, stderr: '', stdout: '', termination: null }
        }
      }),
      /verification failed/
    )
    assert.equal(fs.readFileSync(path.join(existing, 'previous.txt'), 'utf8'), 'keep me')
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('Windows auth runtime recovers an interrupted publication before retrying', () => {
  const fixture = makeFixture()
  const runtimeRoot = path.join(fixture.activeRoot, 'auth-venv')
  const backupName = 'auth-venv.stale-1234-5678'
  const backupRoot = path.join(fixture.activeRoot, backupName)
  const markerPath = path.join(fixture.activeRoot, '.hermes-auth-bootstrap-complete')
  const previousMarker = '{"schemaVersion":1,"scope":"auth"}'

  fs.mkdirSync(runtimeRoot, { recursive: true })
  fs.writeFileSync(path.join(runtimeRoot, 'partial.txt'), 'discard me')
  fs.mkdirSync(backupRoot, { recursive: true })
  fs.writeFileSync(path.join(backupRoot, 'previous.txt'), 'restore me')
  fs.writeFileSync(markerPath, '{"schemaVersion":2,"scope":"auth"}')
  fs.writeFileSync(
    path.join(fixture.activeRoot, 'auth-venv.pending-backup'),
    JSON.stringify({
      schemaVersion: 1,
      backupName,
      markerExisted: true,
      markerBase64: Buffer.from(previousMarker).toString('base64')
    })
  )

  try {
    recoverWindowsAuthRuntimeTransaction(fixture.activeRoot)
    assert.equal(fs.readFileSync(path.join(runtimeRoot, 'previous.txt'), 'utf8'), 'restore me')
    assert.equal(fs.existsSync(path.join(runtimeRoot, 'partial.txt')), false)
    assert.equal(fs.readFileSync(markerPath, 'utf8'), previousMarker)
    assert.equal(fs.existsSync(path.join(fixture.activeRoot, 'auth-venv.pending-backup')), false)
    assert.equal(fs.existsSync(backupRoot), false)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('CLI installers verify bridge and desktop scope protocols before publishing auth readiness', () => {
  const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url))
  const shell = fs.readFileSync(path.join(repoRoot, 'scripts', 'install.sh'), 'utf8')
  const powershell = fs.readFileSync(path.join(repoRoot, 'scripts', 'install.ps1'), 'utf8')

  for (const source of [shell, powershell]) {
    assert.match(source, new RegExp(`bridge\\.PROTOCOL_VERSION == ${AUTH_BRIDGE_PROTOCOL_VERSION}`))
    assert.match(source, new RegExp(`DESKTOP_SCOPE_PROTOCOL_VERSION == ${DESKTOP_SCOPE_PROTOCOL_VERSION}`))
    assert.match(source, /backend_scope_protocol/)
  }

  const markerProtocolVersions = [...powershell.matchAll(/^\s*protocolVersion\s*=\s*(\d+)\s*$/gm)].map(match =>
    Number(match[1])
  )

  assert.deepEqual(markerProtocolVersions, [AUTH_BRIDGE_PROTOCOL_VERSION])
})
