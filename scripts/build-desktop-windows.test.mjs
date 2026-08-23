import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  buildWindowsEnvironment,
  resolveNpmInvocation,
  runWindowsBuild,
  validateWindowsBuildHost,
  windowsBuildCommands
} from './build-desktop-windows.mjs'

const TEST_NPM_INVOCATION = { command: 'node.exe', argsPrefix: ['npm-cli.js'] }

test('host validation accepts only Windows x64 with the pinned Node version', () => {
  assert.doesNotThrow(() =>
    validateWindowsBuildHost({
      platform: 'win32',
      arch: 'x64',
      actualNodeVersion: 'v26.7.0',
      expectedNodeVersion: 'v26.7.0'
    })
  )
  assert.throws(
    () =>
      validateWindowsBuildHost({
        platform: 'darwin',
        arch: 'arm64',
        actualNodeVersion: 'v26.7.0',
        expectedNodeVersion: 'v26.7.0'
      }),
    /Windows x64 is required/
  )
  assert.throws(
    () =>
      validateWindowsBuildHost({
        platform: 'win32',
        arch: 'x64',
        actualNodeVersion: 'v26.6.0',
        expectedNodeVersion: 'v26.7.0'
      }),
    /expected v26\.7\.0, got v26\.6\.0/
  )
})

test('build environment disables Playwright downloads and preserves mirror overrides', () => {
  const defaults = buildWindowsEnvironment({ PATH: 'x' })
  assert.equal(defaults.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD, '1')
  assert.equal(defaults.CSC_IDENTITY_AUTO_DISCOVERY, 'false')
  assert.equal(defaults.ELECTRON_MIRROR, 'https://npmmirror.com/mirrors/electron/')
  assert.equal(defaults.ELECTRON_BUILDER_BINARIES_MIRROR, 'https://npmmirror.com/mirrors/electron-builder-binaries/')
  const custom = buildWindowsEnvironment({
    ELECTRON_MIRROR: 'https://example.test/electron/',
    ELECTRON_BUILDER_BINARIES_MIRROR: 'https://example.test/builder/'
  })
  assert.equal(custom.ELECTRON_MIRROR, 'https://example.test/electron/')
  assert.equal(custom.ELECTRON_BUILDER_BINARIES_MIRROR, 'https://example.test/builder/')
})

test('Windows npm invocation uses node.exe and npm-cli.js instead of spawning npm.cmd', () => {
  assert.deepEqual(
    resolveNpmInvocation({
      platform: 'win32',
      nodeExecutable: 'C:\\nodejs\\node.exe',
      npmExecPath: 'C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js'
    }),
    {
      command: 'C:\\nodejs\\node.exe',
      argsPrefix: ['C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js']
    }
  )
})

test('command plan installs, builds NSIS, then validates without rebuilding', () => {
  const npmInvocation = {
    command: 'C:\\nodejs\\node.exe',
    argsPrefix: ['C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js']
  }
  assert.deepEqual(windowsBuildCommands(npmInvocation), [
    {
      command: 'C:\\nodejs\\node.exe',
      args: ['C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js', 'ci'],
      env: {}
    },
    {
      command: 'C:\\nodejs\\node.exe',
      args: ['C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js', 'run', '--workspace', 'apps/desktop', 'dist:win:nsis'],
      env: {}
    },
    {
      command: 'C:\\nodejs\\node.exe',
      args: [
        'C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js',
        'run',
        '--workspace',
        'apps/desktop',
        'test:desktop:nsis'
      ],
      env: { HERMES_DESKTOP_SKIP_BUILD: '1' }
    }
  ])
})

function writeX64Pe(filePath) {
  const buffer = Buffer.alloc(256)
  buffer.write('MZ', 0, 'ascii')
  buffer.writeUInt32LE(128, 0x3c)
  buffer.write('PE\0\0', 128, 'binary')
  buffer.writeUInt16LE(0x8664, 132)
  fs.writeFileSync(filePath, buffer)
}

test('orchestrator runs all three authoritative commands in order', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ansatz-win-build-'))
  try {
    fs.writeFileSync(path.join(root, '.node-version'), '26.7.0\n')
    const releaseDir = path.join(root, 'apps', 'desktop', 'release')
    const logDir = path.join(root, 'apps', 'desktop', 'build', 'logs')
    fs.mkdirSync(releaseDir, { recursive: true })
    const calls = []
    const runner = async step => {
      calls.push({ args: step.args, skip: step.env.HERMES_DESKTOP_SKIP_BUILD })
      fs.appendFileSync(step.logPath, `${step.command} ${step.args.join(' ')}\n`)
      if (step.args.includes('dist:win:nsis')) {
        const artifact = path.join(releaseDir, 'Ansatz-Voice-Trace-Client-0.17.0-win-x64.exe')
        const appExe = path.join(releaseDir, 'win-unpacked', 'AnsatzVoiceTraceClient.exe')
        fs.mkdirSync(path.dirname(appExe), { recursive: true })
        writeX64Pe(artifact)
        writeX64Pe(appExe)
        fs.utimesSync(artifact, new Date(3_000), new Date(3_000))
      }
    }
    const result = await runWindowsBuild({
      repoRoot: root,
      releaseDir,
      logDir,
      platform: 'win32',
      arch: 'x64',
      actualNodeVersion: 'v26.7.0',
      now: () => 2_000,
      env: {},
      npmInvocation: TEST_NPM_INVOCATION,
      runner
    })
    assert.deepEqual(
      calls.map(call => call.args.at(-1)),
      ['ci', 'dist:win:nsis', 'test:desktop:nsis']
    )
    assert.equal(calls[2].skip, '1')
    assert.match(
      fs.readFileSync(result.artifactPointer, 'utf8'),
      /Ansatz-Voice-Trace-Client-0\.17\.0-win-x64\.exe/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('orchestrator propagates an authoritative command failure', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ansatz-win-build-'))
  try {
    fs.writeFileSync(path.join(root, '.node-version'), '26.7.0\n')
    await assert.rejects(
      runWindowsBuild({
        repoRoot: root,
        platform: 'win32',
        arch: 'x64',
        actualNodeVersion: 'v26.7.0',
        env: {},
        npmInvocation: TEST_NPM_INVOCATION,
        runner: async () => {
          throw new Error('locked install failed')
        }
      }),
      /locked install failed/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('orchestrator rejects a Playwright browser download in the retained log', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ansatz-win-build-'))
  try {
    fs.writeFileSync(path.join(root, '.node-version'), '26.7.0\n')
    const releaseDir = path.join(root, 'apps', 'desktop', 'release')
    fs.mkdirSync(releaseDir, { recursive: true })
    await assert.rejects(
      runWindowsBuild({
        repoRoot: root,
        releaseDir,
        platform: 'win32',
        arch: 'x64',
        actualNodeVersion: 'v26.7.0',
        now: () => 2_000,
        env: {},
        npmInvocation: TEST_NPM_INVOCATION,
        runner: async step => {
          fs.appendFileSync(step.logPath, 'Downloading Chromium 145.0.0 (playwright build v1208)\n')
          if (step.args.includes('dist:win:nsis')) {
            const artifact = path.join(releaseDir, 'Ansatz-Voice-Trace-Client-0.17.0-win-x64.exe')
            const appExe = path.join(releaseDir, 'win-unpacked', 'AnsatzVoiceTraceClient.exe')
            fs.mkdirSync(path.dirname(appExe), { recursive: true })
            writeX64Pe(artifact)
            writeX64Pe(appExe)
            fs.utimesSync(artifact, new Date(3_000), new Date(3_000))
          }
        }
      }),
      /Playwright browser download detected/
    )
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
