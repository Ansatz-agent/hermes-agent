import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  WINDOWS_PYTHON_ARCHIVE,
  WINDOWS_PYTHON_SHA256,
  WINDOWS_PYTHON_SOURCES,
  WINDOWS_PYTHON_VERSION,
  WINDOWS_UV_SHA256,
  WINDOWS_UV_WHEEL,
  WINDOWS_UV_VERSION,
  prepareWindowsAuthToolchainInputs
} from './prepare-auth-toolchain-inputs.mjs'

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-windows-auth-inputs-'))
  const outputDir = path.join(root, 'output')
  const projectRoot = path.join(root, 'repo')
  const authProject = path.join(projectRoot, 'desktop_auth_runtime')
  const hostUvPath = path.join(root, 'uv')
  const hostPythonPath = path.join(root, 'python')

  fs.mkdirSync(authProject, { recursive: true })
  fs.writeFileSync(path.join(authProject, 'pyproject.toml'), '[project]\nname="auth"\n')
  fs.writeFileSync(path.join(authProject, 'uv.lock'), 'version = 1\n')
  fs.writeFileSync(path.join(authProject, 'uv.toml'), 'exclude-newer = "2026-08-19T00:00:00Z"\n')
  fs.writeFileSync(hostUvPath, 'host uv\n')
  fs.writeFileSync(hostPythonPath, 'host Python\n')

  return { root, outputDir, projectRoot, hostUvPath, hostPythonPath }
}

function fixtureCommands({ corruptUv = false, wrongWheel = false } = {}) {
  const calls = []

  function execute(command, args) {
    calls.push({ command, args: [...args] })
    if (args[0] === 'export') {
      const output = args[args.indexOf('--output-file') + 1]
      fs.writeFileSync(
        output,
        `httpx==0.28.1 ; python_version >= '3.13' --hash=sha256:${'a'.repeat(64)}\n` +
          `keyring==25.7.0 --hash=sha256:${'b'.repeat(64)}\n`
      )
      return ''
    }
    if (args.slice(0, 3).join(' ') === '-m pip download') {
      const destination = args[args.indexOf('--dest') + 1]
      fs.mkdirSync(destination, { recursive: true })
      if (args.includes(`uv==${WINDOWS_UV_VERSION}`)) {
        fs.writeFileSync(path.join(destination, WINDOWS_UV_WHEEL), corruptUv ? 'corrupt uv wheel' : 'locked uv wheel')
      } else {
        fs.writeFileSync(path.join(destination, 'httpx-0.28.1-py3-none-any.whl'), 'locked universal wheel')
        fs.writeFileSync(
          path.join(
            destination,
            wrongWheel
              ? 'keyring-25.7.0-cp313-cp313-macosx_11_0_arm64.whl'
              : 'keyring-25.7.0-py3-none-any.whl'
          ),
          'locked keyring wheel'
        )
      }
      return ''
    }
    throw new Error(`unexpected command: ${command} ${args.join(' ')}`)
  }

  return { calls, execute }
}

test('Windows auth toolchain build inputs are exact and domestic-first', () => {
  assert.equal(WINDOWS_PYTHON_VERSION, '3.13.15')
  assert.equal(WINDOWS_PYTHON_ARCHIVE, 'python-3.13.15-embed-amd64.zip')
  assert.deepEqual(WINDOWS_PYTHON_SOURCES, [
    'https://mirrors.huaweicloud.com/python/3.13.15/python-3.13.15-embed-amd64.zip',
    'https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip'
  ])
  assert.equal(WINDOWS_PYTHON_SHA256, 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf')
  assert.equal(WINDOWS_UV_VERSION, '0.12.5')
  assert.equal(WINDOWS_UV_WHEEL, 'uv-0.12.5-py3-none-win_amd64.whl')
  assert.equal(WINDOWS_UV_SHA256, '455c3e57602e2141e66e2f0bf685898c9c5e5a70377d14c9a71554a3baf3ddbf')
})

test('prepareWindowsAuthToolchainInputs exports CPython 3.13 win_amd64 wheels and extracts uv.exe', async () => {
  const fixture = makeFixture()
  const commands = fixtureCommands()
  const downloads = []

  try {
    const result = await prepareWindowsAuthToolchainInputs({
      outputDir: fixture.outputDir,
      projectRoot: fixture.projectRoot,
      hostUvPath: fixture.hostUvPath,
      hostPythonPath: fixture.hostPythonPath,
      execute: commands.execute,
      downloadFile: async ({ sources, destination, expectedSha256 }) => {
        downloads.push({ sources, destination, expectedSha256 })
        fs.writeFileSync(destination, 'locked Python archive')
        return { source: sources[0], sha256: expectedSha256 }
      },
      sha256File: filePath =>
        path.basename(filePath) === WINDOWS_UV_WHEEL ? WINDOWS_UV_SHA256 : WINDOWS_PYTHON_SHA256,
      extractUvExecutable: ({ destination }) => fs.writeFileSync(destination, 'fixture PE32+ x64 uv.exe')
    })

    assert.equal(result.platform, 'win32')
    assert.equal(result.arch, 'x64')
    assert.equal(result.pythonVersion, WINDOWS_PYTHON_VERSION)
    assert.equal(result.uvVersion, WINDOWS_UV_VERSION)
    assert.ok(fs.statSync(result.uvPath).isFile())
    assert.ok(fs.statSync(result.pythonArchivePath).isFile())
    assert.deepEqual(downloads[0].sources, WINDOWS_PYTHON_SOURCES)

    const uvDownload = commands.calls.find(call => call.args.includes(`uv==${WINDOWS_UV_VERSION}`))
    assert.ok(uvDownload)
    assert.equal(
      uvDownload.args[uvDownload.args.indexOf('--index-url') + 1],
      'https://mirrors.ustc.edu.cn/pypi/simple'
    )
    assert.ok(uvDownload.args.includes('--platform'))
    assert.ok(uvDownload.args.includes('win_amd64'))

    const exportCall = commands.calls.find(call => call.args[0] === 'export')
    assert.ok(exportCall)
    assert.equal(
      exportCall.args[exportCall.args.indexOf('--config-file') + 1],
      path.join(fixture.projectRoot, 'desktop_auth_runtime', 'uv.toml')
    )

    const lockedDownload = commands.calls.find(
      call => call.args.slice(0, 3).join(' ') === '-m pip download' && call.args.includes('--requirement')
    )
    assert.ok(lockedDownload)
    assert.ok(lockedDownload.args.includes('--require-hashes'))
    assert.equal(lockedDownload.args[lockedDownload.args.indexOf('--python-version') + 1], '313')
    assert.equal(lockedDownload.args[lockedDownload.args.indexOf('--platform') + 1], 'win_amd64')
    assert.deepEqual(fs.readdirSync(result.wheelhousePath).sort(), [
      'httpx-0.28.1-py3-none-any.whl',
      'keyring-25.7.0-py3-none-any.whl'
    ])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('Windows auth inputs reject corrupt uv and incompatible wheels without publishing', async () => {
  for (const scenario of [{ corruptUv: true }, { wrongWheel: true }]) {
    const fixture = makeFixture()
    const commands = fixtureCommands(scenario)

    try {
      await assert.rejects(
        prepareWindowsAuthToolchainInputs({
          outputDir: fixture.outputDir,
          projectRoot: fixture.projectRoot,
          hostUvPath: fixture.hostUvPath,
          hostPythonPath: fixture.hostPythonPath,
          execute: commands.execute,
          downloadFile: async ({ sources, destination, expectedSha256 }) => {
            fs.writeFileSync(destination, 'locked Python archive')
            return { source: sources[0], sha256: expectedSha256 }
          },
          sha256File: filePath => {
            if (path.basename(filePath) === WINDOWS_UV_WHEEL) {
              return scenario.corruptUv ? '0'.repeat(64) : WINDOWS_UV_SHA256
            }
            return WINDOWS_PYTHON_SHA256
          },
          extractUvExecutable: ({ destination }) => fs.writeFileSync(destination, 'fixture PE32+ x64 uv.exe')
        }),
        scenario.corruptUv ? /uv wheel SHA-256 mismatch/ : /not Windows x64 compatible/
      )
      assert.equal(fs.existsSync(fixture.outputDir), false)
    } finally {
      fs.rmSync(fixture.root, { recursive: true, force: true })
    }
  }
})
