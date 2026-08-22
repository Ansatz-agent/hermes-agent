import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import type { BundledAuthToolchain } from '../bootstrap-toolchain'
import {
  prepareWindowsPackagedAuthRuntime,
  windowsPowerShellExecutable
} from './windows-auth-toolchain'

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

function asset(root: string, file: string): { file: string; size: number; sha256: string }
function asset(root: string, file: string, version: string): { file: string; size: number; sha256: string; version: string }
function asset(root: string, file: string, version?: string) {
  const filePath = path.join(root, file)
  const value = { file, size: fs.statSync(filePath).size, sha256: sha256(filePath) }
  return version ? { ...value, version } : value
}

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-windows-auth-runtime-'))
  const toolchainRoot = path.join(root, 'toolchain')
  const activeRoot = path.join(root, 'hermes-agent')
  fs.mkdirSync(path.join(toolchainRoot, 'wheelhouse'), { recursive: true })
  fs.mkdirSync(path.join(activeRoot, 'hermes_cli', 'client_auth'), { recursive: true })
  writePeX64(path.join(toolchainRoot, 'uv.exe'))
  fs.writeFileSync(path.join(toolchainRoot, 'python-embed.zip'), 'fixture zip')
  fs.writeFileSync(path.join(toolchainRoot, 'auth-requirements.txt'), `keyring==25.7.0 --hash=sha256:${'a'.repeat(64)}\n`)
  fs.writeFileSync(path.join(toolchainRoot, 'wheelhouse', 'keyring-25.7.0-py3-none-any.whl'), 'wheel')
  const manifest = {
    schemaVersion: 1 as const,
    platform: 'win32' as const,
    arch: 'x64' as const,
    uv: asset(toolchainRoot, 'uv.exe', '0.12.5'),
    python: asset(toolchainRoot, 'python-embed.zip', '3.13.15'),
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

  try {
    const result = await prepareWindowsPackagedAuthRuntime({
      toolchain: fixture.toolchain,
      activeRoot: fixture.activeRoot,
      env: { SystemRoot: 'C:\\Windows', LOCALAPPDATA: 'C:\\Users\\tester\\AppData\\Local' },
      runProcess: async options => {
        calls.push(options)
        if (options.args.includes('Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force')) {
          const destination = options.args.at(-1)
          fs.mkdirSync(destination, { recursive: true })
          writePeX64(path.join(destination, 'python.exe'))
          fs.writeFileSync(path.join(destination, 'python313.zip'), 'stdlib')
        }
        return { code: 0, killed: false, signal: null, stderr: '', stdout: '', termination: null }
      }
    })

    assert.equal(result.runtimeRoot, path.join(fixture.activeRoot, 'venv'))
    assert.equal(windowsPowerShellExecutable({ SystemRoot: 'C:\\Windows' }), 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe')
    const expand = calls[0]
    assert.equal(expand.command, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe')
    assert.ok(expand.args.includes('-NoProfile'))
    assert.ok(expand.args.includes('-NonInteractive'))
    assert.ok(expand.hardTimeoutMs > 0)
    assert.ok(expand.idleTimeoutMs > 0)

    const install = calls.find(call => call.args[0] === 'pip')
    assert.ok(install)
    assert.ok(install.args.includes('--no-index'))
    assert.ok(install.args.includes('--require-hashes'))
    assert.equal(install.args.some(arg => /^https?:/i.test(arg)), false)
    assert.match(fs.readFileSync(path.join(result.runtimeRoot, 'python313._pth'), 'utf8'), /^python313\.zip\n\.\nLib\\site-packages\n\.\.\nimport site\n$/)
    assert.ok(fs.statSync(path.join(result.runtimeRoot, 'python.exe')).isFile())
    assert.ok(fs.statSync(result.managedUvPath).isFile())
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
        runProcess: async options => {
          if (options.args.includes('Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force')) {
            const destination = options.args.at(-1)
            fs.mkdirSync(destination, { recursive: true })
            fs.writeFileSync(path.join(destination, 'python.exe'), 'wrong architecture')
          }
          return { code: 0, killed: false, signal: null, stderr: '', stdout: '', termination: null }
        }
      }),
      /Python executable is not PE32\+ x64/
    )
    assert.equal(fs.existsSync(path.join(fixture.activeRoot, 'venv')), false)
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
  const existing = path.join(fixture.activeRoot, 'venv')
  fs.mkdirSync(existing, { recursive: true })
  fs.writeFileSync(path.join(existing, 'previous.txt'), 'keep me')

  try {
    await assert.rejects(
      prepareWindowsPackagedAuthRuntime({
        toolchain: fixture.toolchain,
        activeRoot: fixture.activeRoot,
        env: { SystemRoot: 'C:\\Windows' },
        runProcess: async options => {
          if (options.args.includes('Expand-Archive -LiteralPath $args[0] -DestinationPath $args[1] -Force')) {
            const destination = options.args.at(-1)
            fs.mkdirSync(destination, { recursive: true })
            writePeX64(path.join(destination, 'python.exe'))
            fs.writeFileSync(path.join(destination, 'python313.zip'), 'stdlib')
          }
          if (options.command.endsWith('python.exe')) {
            return { code: 1, killed: false, signal: null, stderr: 'verification failed', stdout: '', termination: null }
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
