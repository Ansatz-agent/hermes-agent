import assert from 'node:assert/strict'
import { type ChildProcessWithoutNullStreams, spawn } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import { ansatzAuthEnvironment } from './ansatz-product'
import { spawnUpdaterProcess } from './updater-process'

const repoRoot = fileURLToPath(new URL('../../..', import.meta.url))

const python = process.env.HERMES_PYTHON || path.join(
  repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'
)

const guardProbe = `
import sys
from hermes_cli.client_auth.guard import enforce_raw_argv, GuardRejected
try:
    enforce_raw_argv(sys.argv[1:])
except GuardRejected as error:
    print(error.reason)
    raise SystemExit(20)
print('authorized')
`

async function output(child: ChildProcessWithoutNullStreams) {
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', chunk => { stdout += chunk.toString() })
  child.stderr.on('data', chunk => { stderr += chunk.toString() })
  const [code] = await once(child, 'close')

  return { code, stdout: stdout.trim(), stderr }
}

for (const state of ['authenticated', 'signed-out']) {
  test(`detached updater reaches the Desktop auth owner and preserves ${state} authorization`, async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'updater-auth-'))

    const environment: NodeJS.ProcessEnv = {
      PATH: process.env.PATH,
      HOME: home,
      HERMES_HOME: home,
      HERMES_TEST_ISOLATION: home,
      PYTHONPATH: repoRoot,
      PYTHONUTF8: '1',
      SYSTEMROOT: process.env.SYSTEMROOT,
      LOCALAPPDATA: process.env.LOCALAPPDATA,
      TEMP: process.env.TEMP,
      TMPDIR: process.env.TMPDIR
    }

    const owner = spawn(python, [
      '-u', path.join(repoRoot, 'tests/fixtures/desktop_update_auth_owner.py'), state
    ], { cwd: repoRoot, env: ansatzAuthEnvironment(home, environment), stdio: 'pipe', windowsHide: true })

    const ownerExited = once(owner, 'close')
    let ownerErrors = ''
    owner.stderr.on('data', chunk => { ownerErrors += chunk.toString() })

    try {
      const ready = await Promise.race([
        once(owner.stdout, 'data').then(([chunk]) => chunk.toString().trim()),
        ownerExited.then(() => { throw new Error(ownerErrors || 'auth owner exited before readiness') })
      ])

      assert.equal(ready, 'ready')
      const args = ['-c', guardProbe, 'update', '--yes', '--gateway', '--branch', 'main']

      // The old Desktop launch options reach the default Hermes namespace.
      const before = await output(spawn(python, args, {
        cwd: repoRoot, env: environment, stdio: 'pipe', windowsHide: true
      }))

      assert.equal(before.code, 20)
      assert.equal(before.stdout, 'runtime_unavailable')

      // Exercise the real spawn boundary and the unchanged Python auth guard.
      const after = await output(spawnUpdaterProcess(python, args, {
        cwd: repoRoot, env: environment, detached: true, stdio: 'pipe'
      }) as ChildProcessWithoutNullStreams)

      assert.equal(after.code, state === 'authenticated' ? 0 : 20, after.stderr)
      assert.equal(after.stdout, state === 'authenticated' ? 'authorized' : 'signed_out')
      assert.equal(environment.HERMES_AUTH_RUNTIME_NAMESPACE, undefined)
      assert.equal(environment.ANSATZ_EXTERNAL_AUTH, undefined)
    } finally {
      owner.stdin.end()
      await ownerExited
      fs.rmSync(home, { recursive: true, force: true })
    }
  }, 20_000)
}
