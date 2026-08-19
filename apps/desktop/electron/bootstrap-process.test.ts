import assert from 'node:assert/strict'

import { test } from 'vitest'

import { buildBootstrapEnvironment, runBootstrapProcess } from './bootstrap-process'

test('buildBootstrapEnvironment drops inherited package-manager redirects and Python injection', () => {
  const env = buildBootstrapEnvironment(
    {
      HOME: '/Users/example',
      PATH: '/usr/bin:/bin',
      LANG: 'en_US.UTF-8',
      UV_INDEX_URL: 'https://attacker.invalid/simple',
      UV_PYTHON: '/tmp/attacker-python',
      PIP_INDEX_URL: 'https://attacker.invalid/pypi',
      PIP_CONFIG_FILE: '/tmp/pip.conf',
      npm_config_registry: 'https://attacker.invalid/npm',
      NPM_CONFIG_USERCONFIG: '/tmp/npmrc',
      PYTHONPATH: '/tmp/injected',
      PYTHONHOME: '/tmp/injected-home',
      PLAYWRIGHT_DOWNLOAD_HOST: 'https://attacker.invalid/playwright'
    },
    { hermesHome: '/Users/example/.hermes', npmConfigPath: '/signed/empty.npmrc' }
  )

  assert.equal(env.HOME, '/Users/example')
  assert.equal(env.PATH, '/usr/bin:/bin')
  assert.equal(env.HERMES_HOME, '/Users/example/.hermes')
  assert.equal(env.UV_NO_CONFIG, '1')
  assert.equal(env.PIP_CONFIG_FILE, '/dev/null')
  assert.equal(env.NPM_CONFIG_USERCONFIG, '/signed/empty.npmrc')
  assert.equal(env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD, '1')
  assert.equal(env.UV_INDEX_URL, undefined)
  assert.equal(env.UV_PYTHON, undefined)
  assert.equal(env.PIP_INDEX_URL, undefined)
  assert.equal(env.npm_config_registry, undefined)
  assert.equal(env.PYTHONPATH, undefined)
  assert.equal(env.PYTHONHOME, undefined)
  assert.equal(env.PLAYWRIGHT_DOWNLOAD_HOST, undefined)
})

test('runBootstrapProcess drains large stdout and stderr while bounding captured output', async () => {
  const result = await runBootstrapProcess({
    command: process.execPath,
    args: [
      '-e',
      "process.stdout.write('o'.repeat(1024*1024+17)); process.stderr.write('e'.repeat(1024*1024+19))"
    ],
    captureLimitBytes: 128 * 1024,
    hardTimeoutMs: 5_000,
    idleTimeoutMs: 2_000,
    killGraceMs: 50
  })

  assert.equal(result.code, 0)
  assert.equal(result.termination, null)
  assert.ok(Buffer.byteLength(result.stdout) <= 128 * 1024)
  assert.ok(Buffer.byteLength(result.stderr) <= 128 * 1024)
  assert.ok(result.stdout.endsWith('o'.repeat(64)))
  assert.ok(result.stderr.endsWith('e'.repeat(64)))
})

test('runBootstrapProcess turns an idle child into a terminal idle timeout', async () => {
  const startedAt = Date.now()

  const result = await runBootstrapProcess({
    command: process.execPath,
    args: ['-e', "process.stdout.write('ready\\n'); setInterval(() => {}, 1000)"],
    hardTimeoutMs: 2_000,
    idleTimeoutMs: 100,
    killGraceMs: 50
  })

  assert.equal(result.termination, 'idle-timeout')
  assert.ok(Date.now() - startedAt < 1_000)
})

test('runBootstrapProcess enforces a hard deadline even while output is active', async () => {
  const result = await runBootstrapProcess({
    command: process.execPath,
    args: ['-e', "setInterval(() => process.stdout.write('progress\\n'), 20)"],
    hardTimeoutMs: 150,
    idleTimeoutMs: 1_000,
    killGraceMs: 50
  })

  assert.equal(result.termination, 'hard-timeout')
})

test('runBootstrapProcess cancellation reaches a terminal result', async () => {
  const controller = new AbortController()

  const pending = runBootstrapProcess({
    command: process.execPath,
    args: ['-e', 'setInterval(() => {}, 1000)'],
    abortSignal: controller.signal,
    hardTimeoutMs: 2_000,
    idleTimeoutMs: 1_000,
    killGraceMs: 50
  })

  setTimeout(() => controller.abort(), 50)
  const result = await pending

  assert.equal(result.termination, 'cancelled')
})
