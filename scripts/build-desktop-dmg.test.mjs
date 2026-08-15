import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import test from 'node:test'

const repoRoot = path.resolve(import.meta.dirname, '..')
const buildScript = path.join(repoRoot, 'scripts', 'build-desktop-dmg.sh')

function runCheck(version) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-node-'))
  const fakeNode = path.join(tempRoot, 'node')
  fs.writeFileSync(fakeNode, `#!/bin/sh\nprintf '%s\\n' '${version}'\n`, { mode: 0o755 })
  try {
    return spawnSync('/bin/bash', [buildScript, '--check'], {
      cwd: repoRoot,
      env: { ...process.env, PATH: `${tempRoot}:/usr/bin:/bin` },
      encoding: 'utf8'
    })
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true })
  }
}

test('preflight rejects a Node version other than 26.7.0', () => {
  const result = runCheck('v25.1.0')
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /expected v26\.7\.0, got v25\.1\.0/)
})

test('preflight accepts Node 26.7.0 and disables Playwright downloads', () => {
  const result = runCheck('v26.7.0')
  assert.equal(result.status, 0, result.stderr)
  assert.match(result.stdout, /PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1/)
})
