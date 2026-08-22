import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildBootstrapEnvironment,
  DOMESTIC_BOOTSTRAP_MIRRORS,
  runBootstrapProcess
} from './bootstrap-process'

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

test('buildBootstrapEnvironment replaces hostile package redirects with the fixed domestic runtime policy', () => {
  const env = buildBootstrapEnvironment(
    {
      HOME: '/Users/example',
      PATH: '/usr/bin:/bin',
      UV_DEFAULT_INDEX: 'https://attacker.invalid/pypi',
      HERMES_UV_FALLBACK_INDEX: 'https://attacker.invalid/fallback',
      NPM_CONFIG_REGISTRY: 'https://attacker.invalid/npm',
      HERMES_NODE_MIRROR: 'https://attacker.invalid/node',
      PLAYWRIGHT_DOWNLOAD_HOST: 'https://attacker.invalid/playwright'
    },
    { hermesHome: '/Users/example/.hermes', useDomesticRuntimeMirrors: true }
  )

  assert.deepEqual(DOMESTIC_BOOTSTRAP_MIRRORS, {
    pythonPrimary: 'https://mirrors.ustc.edu.cn/pypi/simple',
    pythonFallback: 'https://pypi.tuna.tsinghua.edu.cn/simple',
    npmRegistry: 'https://registry.npmmirror.com',
    nodeBase: 'https://registry.npmmirror.com/-/binary/node/',
    playwrightBase: 'https://registry.npmmirror.com/-/binary/playwright/'
  })
  assert.equal(env.UV_DEFAULT_INDEX, DOMESTIC_BOOTSTRAP_MIRRORS.pythonPrimary)
  assert.equal(env.HERMES_UV_FALLBACK_INDEX, DOMESTIC_BOOTSTRAP_MIRRORS.pythonFallback)
  assert.equal(env.NPM_CONFIG_REGISTRY, DOMESTIC_BOOTSTRAP_MIRRORS.npmRegistry)
  assert.equal(env.HERMES_NODE_MIRROR, DOMESTIC_BOOTSTRAP_MIRRORS.nodeBase)
  assert.equal(env.PLAYWRIGHT_DOWNLOAD_HOST, DOMESTIC_BOOTSTRAP_MIRRORS.playwrightBase)
  assert.equal(JSON.stringify(env).includes('attacker.invalid'), false)
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

test('runBootstrapProcess heartbeat keeps a silent active package stage alive', async () => {
  const events: any[] = []
  const controller = new AbortController()

  const result = await runBootstrapProcess({
    command: process.execPath,
    args: ['-e', 'setInterval(() => {}, 1_000)'],
    abortSignal: controller.signal,
    emit: event => {
      events.push(event)

      if (events.filter(candidate => candidate.type === 'progress').length >= 4) {
        controller.abort()
      }
    },
    stageName: 'python-deps',
    hardTimeoutMs: 5_000,
    idleTimeoutMs: 250,
    killGraceMs: 50,
    progressHeartbeatMs: 100
  })

  // Four heartbeats span longer than the original 250 ms idle deadline. The
  // controller ends the child from an observed event instead of racing a
  // sub-second child-exit timer on a busy test runner.
  assert.equal(result.termination, 'cancelled')
  assert.ok(
    events.some(
      event =>
        event.type === 'progress' &&
        event.stage === 'python-deps' &&
        event.completed === 0 &&
        event.total === null &&
        event.unit === 'items' &&
        event.label === 'python-deps' &&
        typeof event.updatedAt === 'number'
    )
  )
})

test('runBootstrapProcess heartbeat never weakens the hard deadline', async () => {
  const result = await runBootstrapProcess({
    command: process.execPath,
    args: ['-e', 'setInterval(() => {}, 1_000)'],
    stageName: 'node-deps',
    hardTimeoutMs: 150,
    idleTimeoutMs: 80,
    killGraceMs: 50,
    progressHeartbeatMs: 30
  })

  assert.equal(result.termination, 'hard-timeout')
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

test('runBootstrapProcess emits reserved structured progress without exposing the frame as a log', async () => {
  const events: any[] = []

  const frame = {
    type: 'progress',
    stage: 'python-deps',
    completed: 38_200_000,
    total: 126_500_000,
    unit: 'bytes',
    label: 'Hermes Python dependencies'
  }

  const result = await runBootstrapProcess({
    command: process.execPath,
    args: [
      '-e',
      `process.stdout.write(${JSON.stringify(`HERMES_BOOTSTRAP_PROGRESS ${JSON.stringify(frame)}\n`)})`
    ],
    emit: event => events.push(event),
    stageName: 'python-deps',
    hardTimeoutMs: 2_000,
    idleTimeoutMs: 1_000,
    killGraceMs: 50
  })

  assert.equal(result.code, 0)
  assert.equal(events.some(event => event.type === 'log' && event.line.includes('HERMES_BOOTSTRAP_PROGRESS')), false)
  assert.deepEqual(events, [{ ...frame, updatedAt: events[0]?.updatedAt }])
  assert.equal(typeof events[0]?.updatedAt, 'number')
})

test('runBootstrapProcess never derives progress from installer prose', async () => {
  const events: any[] = []

  await runBootstrapProcess({
    command: process.execPath,
    args: [
      '-e',
      `process.stdout.write(${JSON.stringify(
        'Downloading 38.2 MB / 126.5 MB (30%)\nuv resolved 47 packages\nnpm progress 80%\n'
      )})`
    ],
    emit: event => events.push(event),
    stageName: 'python-deps',
    hardTimeoutMs: 2_000,
    idleTimeoutMs: 1_000,
    killGraceMs: 50
  })

  assert.equal(events.filter(event => event.type === 'progress').length, 0)
  assert.deepEqual(
    events.filter(event => event.type === 'log').map(event => event.line),
    ['Downloading 38.2 MB / 126.5 MB (30%)', 'uv resolved 47 packages', 'npm progress 80%']
  )
})

test('runBootstrapProcess swallows malformed or wrong-stage structured frames and sanitizes hostile labels', async () => {
  const events: any[] = []

  const lines = [
    'HERMES_BOOTSTRAP_PROGRESS not-json',
    `HERMES_BOOTSTRAP_PROGRESS ${JSON.stringify({
      type: 'progress',
      stage: 'other-stage',
      completed: 1,
      total: 2,
      unit: 'items',
      label: 'Wrong stage'
    })}`,
    `HERMES_BOOTSTRAP_PROGRESS ${JSON.stringify({
      type: 'progress',
      stage: 'python-deps',
      completed: 47,
      total: null,
      unit: 'packages',
      label: 'password=secret Cookie: abc /Users/alice/private'
    })}`
  ]

  await runBootstrapProcess({
    command: process.execPath,
    args: ['-e', `process.stdout.write(${JSON.stringify(`${lines.join('\n')}\n`)})`],
    emit: event => events.push(event),
    stageName: 'python-deps',
    hardTimeoutMs: 2_000,
    idleTimeoutMs: 1_000,
    killGraceMs: 50
  })

  assert.equal(events.some(event => event.type === 'log'), false)
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'progress')
  assert.equal(events[0].stage, 'python-deps')
  assert.equal(events[0].total, null)
  assert.equal(events[0].label, 'python-deps')
  assert.equal(JSON.stringify(events).includes('secret'), false)
  assert.equal(JSON.stringify(events).includes('/Users/'), false)
})
