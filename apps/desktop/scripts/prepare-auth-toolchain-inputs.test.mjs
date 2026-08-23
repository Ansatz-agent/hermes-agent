import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildAuthLockEnvironment,
  buildAuthPayloadEnvironment,
  findManagedPython,
  findUv,
  prepareAuthToolchainInputs
} from './prepare-auth-toolchain-inputs.mjs'

test('auth payload environment rejects inherited downloader redirects and credentials', () => {
  const env = buildAuthPayloadEnvironment({
    HOME: '/Users/example',
    PATH: '/usr/bin:/bin',
    PIP_INDEX_URL: 'https://attacker.invalid/pypi',
    PIP_EXTRA_INDEX_URL: 'https://attacker.invalid/extra',
    UV_CONFIG_FILE: '/tmp/attacker.toml',
    UV_PYTHON_INSTALL_DIR: 'D:\\a\\_temp\\uv-python-dir',
    HF_TOKEN: 'secret-token',
    PYTHONPATH: '/tmp/injected'
  })
  assert.equal(env.UV_DEFAULT_INDEX, 'https://mirrors.ustc.edu.cn/pypi/simple')
  assert.equal(env.HERMES_UV_FALLBACK_INDEX, 'https://pypi.tuna.tsinghua.edu.cn/simple')
  assert.equal(env.PIP_INDEX_URL, 'https://mirrors.ustc.edu.cn/pypi/simple')
  assert.equal(env.PIP_EXTRA_INDEX_URL, undefined)
  assert.equal(env.UV_CONFIG_FILE, undefined)
  assert.equal(env.UV_PYTHON_INSTALL_DIR, 'D:\\a\\_temp\\uv-python-dir')
  assert.equal(env.HF_TOKEN, undefined)
  assert.equal(env.PYTHONPATH, undefined)
  assert.equal(JSON.stringify(env).includes('attacker.invalid'), false)
  assert.equal(JSON.stringify(env).includes('secret-token'), false)

  const lockEnv = buildAuthLockEnvironment({ HOME: '/Users/example', PATH: '/usr/bin:/bin' })
  assert.equal(lockEnv.UV_NO_CONFIG, '1')
  assert.equal(lockEnv.UV_OFFLINE, '1')
  assert.equal(lockEnv.UV_DEFAULT_INDEX, undefined)
  assert.equal(lockEnv.UV_INDEX, undefined)
  assert.equal(lockEnv.PIP_INDEX_URL, undefined)
})

test('managed Python discovery ignores the project venv and resolves interpreter links', () => {
  const calls = []
  const execute = (command, args) => {
    calls.push({ command, args })
    return '/Users/example/.local/share/uv/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11\n'
  }

  assert.equal(
    findManagedPython('/opt/hermes/uv', '3.11', execute),
    '/Users/example/.local/share/uv/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11'
  )
  assert.deepEqual(calls, [{
    command: '/opt/hermes/uv',
    args: ['python', 'find', '--no-project', '--managed-python', '--resolve-links', '3.11']
  }])
})

test('Windows uv discovery falls back to where.exe', () => {
  const calls = []
  const uvPath = 'C:\\hostedtoolcache\\uv\\0.12.5\\x64\\uv.exe'

  const result = findUv(
    { USERPROFILE: 'C:\\Users\\runneradmin' },
    'C:\\repo',
    {
      platform: 'win32',
      existsSync: candidate => candidate === uvPath,
      execute: (command, args) => {
        calls.push({ command, args })
        return `${uvPath}\r\n`
      }
    }
  )

  assert.equal(result, uvPath)
  assert.deepEqual(calls, [{ command: 'where.exe', args: ['uv'] }])
})

test('Windows uv discovery reads the mixed-case runner Path before spawning a locator', () => {
  const calls = []
  const uvPath = 'C:\\hostedtoolcache\\uv\\0.12.5\\x64\\uv.exe'

  const result = findUv(
    { Path: `C:\\Windows\\System32;C:\\hostedtoolcache\\uv\\0.12.5\\x64` },
    'C:\\repo',
    {
      platform: 'win32',
      existsSync: candidate => candidate === uvPath,
      execute: (command, args) => {
        calls.push({ command, args })
        throw new Error('secondary locator unavailable')
      }
    }
  )

  assert.equal(result, uvPath)
  assert.deepEqual(calls, [])
})

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-inputs-'))
  const outputDir = path.join(root, 'output')
  const uvPath = path.join(root, 'uv')
  const pythonRoot = path.join(root, 'cpython-3.11.16-macos-aarch64-none')
  const pythonPath = path.join(pythonRoot, 'bin', 'python3.11')
  const projectRoot = path.join(root, 'repo')

  fs.mkdirSync(path.dirname(pythonPath), { recursive: true })
  fs.mkdirSync(path.join(projectRoot, 'desktop_auth_runtime'), { recursive: true })
  fs.writeFileSync(uvPath, 'fixture uv\n', { mode: 0o755 })
  fs.writeFileSync(pythonPath, 'fixture python\n', { mode: 0o755 })
  fs.writeFileSync(path.join(projectRoot, 'desktop_auth_runtime', 'uv.lock'), 'version = 1\n')
  fs.writeFileSync(path.join(projectRoot, 'desktop_auth_runtime', 'uv.toml'), 'exclude-newer = "2026-08-19T00:00:00Z"\n')
  fs.writeFileSync(path.join(projectRoot, 'desktop_auth_runtime', 'pyproject.toml'), '[project]\nname="auth"\n')

  return { root, outputDir, uvPath, pythonRoot, pythonPath, projectRoot }
}

function commandDouble({ failPrimary = false, wrongUvArch = false } = {}) {
  const calls = []

  function execute(command, args, options = {}) {
    calls.push({ command, args: [...args], options })

    if (command === '/usr/bin/file') {
      return wrongUvArch && args.at(-1)?.endsWith('/uv')
        ? 'Mach-O 64-bit executable x86_64\n'
        : 'Mach-O 64-bit executable arm64\n'
    }
    if (args[0] === '--version') {
      return 'uv 0.12.5 (fixture arm64-apple-darwin)\n'
    }
    if (args[0] === '-c') {
      return '3.11.16\n'
    }
    if (command === '/usr/bin/tar') {
      fs.writeFileSync(args[1], 'fixture python archive\n')
      return ''
    }
    if (args[0] === 'export') {
      const output = args[args.indexOf('--output-file') + 1]
      fs.writeFileSync(output, `httpx==0.28.1 --hash=sha256:${'a'.repeat(64)}\n`)
      return ''
    }
    if (args.slice(0, 3).join(' ') === '-m pip download') {
      const index = args[args.indexOf('--index-url') + 1]
      if (failPrimary && index === 'https://mirrors.ustc.edu.cn/pypi/simple') {
        throw new Error('primary unavailable')
      }
      const wheelhouse = args[args.indexOf('--dest') + 1]
      fs.writeFileSync(path.join(wheelhouse, 'httpx-0.28.1-py3-none-any.whl'), 'fixture wheel\n')
      return ''
    }

    throw new Error(`unexpected command: ${command} ${args.join(' ')}`)
  }

  return { calls, execute }
}

test('prepareAuthToolchainInputs creates locked macOS arm64 build inputs from approved sources', () => {
  const fixture = makeFixture()
  const commands = commandDouble()

  try {
    const result = prepareAuthToolchainInputs({
      outputDir: fixture.outputDir,
      projectRoot: fixture.projectRoot,
      pythonPath: fixture.pythonPath,
      uvPath: fixture.uvPath,
      execute: commands.execute
    })

    assert.equal(result.uvVersion, '0.12.5')
    assert.equal(result.pythonVersion, '3.11.16')
    assert.ok(fs.statSync(result.uvPath).isFile())
    assert.ok(fs.statSync(result.pythonArchivePath).isFile())
    assert.ok(fs.statSync(result.requirementsPath).isFile())
    assert.equal(fs.readdirSync(result.wheelhousePath).length, 1)
    assert.ok(fs.statSync(path.join(fixture.outputDir, 'metadata.json')).isFile())

    const exportCall = commands.calls.find(call => call.args[0] === 'export')
    assert.ok(exportCall)
    assert.ok(exportCall.args.includes('--locked'))
    assert.ok(exportCall.args.includes('--no-emit-project'))
    assert.equal(
      exportCall.args[exportCall.args.indexOf('--config-file') + 1],
      path.join(fixture.projectRoot, 'desktop_auth_runtime', 'uv.toml')
    )
    assert.equal(exportCall.options.env.UV_OFFLINE, '1')
    assert.equal(exportCall.options.env.UV_DEFAULT_INDEX, undefined)
    assert.equal(exportCall.options.env.UV_INDEX, undefined)

    const downloadCall = commands.calls.find(call => call.args.slice(0, 3).join(' ') === '-m pip download')
    assert.ok(downloadCall)
    assert.ok(downloadCall.args.includes('--require-hashes'))
    assert.ok(downloadCall.args.includes('--only-binary=:all:'))
    assert.equal(
      downloadCall.args[downloadCall.args.indexOf('--index-url') + 1],
      'https://mirrors.ustc.edu.cn/pypi/simple'
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('prepareAuthToolchainInputs retries wheel acquisition only on the approved fallback mirror', () => {
  const fixture = makeFixture()
  const commands = commandDouble({ failPrimary: true })

  try {
    prepareAuthToolchainInputs({
      outputDir: fixture.outputDir,
      projectRoot: fixture.projectRoot,
      pythonPath: fixture.pythonPath,
      uvPath: fixture.uvPath,
      execute: commands.execute
    })

    const indexes = commands.calls
      .filter(call => call.args.slice(0, 3).join(' ') === '-m pip download')
      .map(call => call.args[call.args.indexOf('--index-url') + 1])
    assert.deepEqual(indexes, [
      'https://mirrors.ustc.edu.cn/pypi/simple',
      'https://pypi.tuna.tsinghua.edu.cn/simple'
    ])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('prepareAuthToolchainInputs rejects a non-arm64 uv executable', () => {
  const fixture = makeFixture()
  const commands = commandDouble({ wrongUvArch: true })

  try {
    assert.throws(
      () =>
        prepareAuthToolchainInputs({
          outputDir: fixture.outputDir,
          projectRoot: fixture.projectRoot,
          pythonPath: fixture.pythonPath,
          uvPath: fixture.uvPath,
          execute: commands.execute
        }),
      /uv executable must be macOS arm64/
    )
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})
