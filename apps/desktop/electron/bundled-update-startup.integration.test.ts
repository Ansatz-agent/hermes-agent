import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import { readBundledSourceMarker } from './bootstrap-payload'
import { classifyBundledRuntime } from './bundled-runtime-state'

const repoRoot = fileURLToPath(new URL('../../..', import.meta.url))

const python = process.env.HERMES_PYTHON || path.join(
  repoRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'
)

test('Python Git adoption leaves a source identity Desktop can start without another bootstrap', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'bundled-update-startup-'))

  try {
    const result = execFileSync(python, ['-c', `
import json, subprocess, sys
from pathlib import Path
from hermes_cli.bundled_update import adopt_bundled_checkout, SOURCE_MARKER

def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()

temporary = Path(sys.argv[1])
remote = temporary / 'remote'
remote.mkdir()
git(remote, 'init', '-b', 'main')
git(remote, 'config', 'user.name', 'Update test')
git(remote, 'config', 'user.email', 'update@example.invalid')
for name in ('hermes_cli/main.py', 'pyproject.toml', 'apps/desktop/package.json'):
    file = remote / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('{}', encoding='utf-8')
git(remote, 'add', '.')
git(remote, 'commit', '-m', 'base')
current = git(remote, 'rev-parse', 'HEAD')
git(remote, 'commit', '--allow-empty', '-m', 'published update')
target = git(remote, 'rev-parse', 'HEAD')
root = temporary / 'home/hermes-agent'
root.mkdir(parents=True)
(root / SOURCE_MARKER).write_text(json.dumps({
    'schemaVersion': 1, 'commit': current,
    'archiveSha256': 'a' * 64, 'installedAt': '2026-09-07T00:00:00Z',
}), encoding='utf-8')
(root / '.install_method').write_text('desktop-bundle', encoding='utf-8')
backup = adopt_bundled_checkout(root, 'main', current, target, repository_url=str(remote))
git(root, 'merge', '--ff-only', 'origin/main')
assert git(root, 'rev-parse', 'HEAD') == target
print(json.dumps({'root': str(root), 'backup': str(backup), 'current': current, 'target': target}))
`, temporary], {
      cwd: repoRoot,
      env: { ...process.env, PYTHONPATH: repoRoot, HERMES_HOME: path.join(temporary, 'home') },
      encoding: 'utf8',
      timeout: 15_000
    })

    const { root, backup, current, target } = JSON.parse(result)
    const source = readBundledSourceMarker(root)

    assert.equal(readBundledSourceMarker(backup)?.commit, current)

    // Both the replacement app and the previous app can reuse the Git runtime.
    for (const payloadCommit of [target, current]) {
      assert.equal(classifyBundledRuntime({
        packaged: true,
        runtimeUsable: true,
        installMethod: fs.readFileSync(path.join(root, '.install_method'), 'utf8').trim(),
        sourceCommit: source?.commit,
        payloadCommit
      }), 'not-applicable')
    }

    // A pre-fix updater runs its old adoption code before pulling the new app.
    fs.copyFileSync(
      path.join(backup, '.hermes-bundled-source.json'),
      path.join(root, '.hermes-bundled-source.json')
    )

    const legacyState = {
      packaged: true,
      runtimeUsable: true,
      installMethod: fs.readFileSync(path.join(root, '.install_method'), 'utf8').trim(),
      sourceCommit: readBundledSourceMarker(root)?.commit,
      payloadCommit: target,
      gitCheckout: fs.statSync(path.join(root, '.git')).isDirectory()
    }

    assert.equal(classifyBundledRuntime(legacyState), 'not-applicable')
    assert.equal(classifyBundledRuntime({ ...legacyState, gitCheckout: false }), 'refresh')
    assert.equal(classifyBundledRuntime({ ...legacyState, transactionPending: true }), 'refresh')
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true })
  }
}, 20_000)
