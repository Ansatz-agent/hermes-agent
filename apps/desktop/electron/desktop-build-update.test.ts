import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { reconcileDesktopBuild } from './desktop-build-update'

test('a real updated Git checkout stays actionable until the installed app catches up', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-rebuild-'))
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim()

  const isAncestor = async (older: string, newer: string) => spawnSync(
    'git', ['merge-base', '--is-ancestor', older, newer], { cwd: root }
  ).status === 0

  try {
    git('init', '--quiet')
    git('config', 'user.name', 'Desktop Test')
    git('config', 'user.email', 'desktop@example.invalid')
    git('config', 'commit.gpgSign', 'false')
    git('config', 'core.hooksPath', '.git/no-hooks')
    git('commit', '--allow-empty', '-qm', 'installed app')
    const installedSha = git('rev-parse', 'HEAD')
    git('commit', '--allow-empty', '-qm', 'downloaded update')
    const targetSha = git('rev-parse', 'HEAD')
    const status = { supported: true, updateAvailable: false, behind: 0, currentSha: targetSha, targetSha }

    const incomplete = await reconcileDesktopBuild(status, { managedPackagedApp: true, installedSha, isAncestor })
    assert.equal(incomplete.updateAvailable, true)
    assert.equal(incomplete.currentSha, installedSha)
    assert.equal(incomplete.behind, null)
    assert.equal('reason' in incomplete && incomplete.reason, 'desktop-rebuild-required')

    const completed = await reconcileDesktopBuild(status, {
      managedPackagedApp: true, installedSha: targetSha, isAncestor
    })

    assert.equal(completed, status)

    // Installing a newer branch build must not offer a downgrade to main.
    const olderSource = { ...status, currentSha: installedSha, targetSha: installedSha }
    assert.equal(await reconcileDesktopBuild(olderSource, {
      managedPackagedApp: true, installedSha: targetSha, isAncestor
    }), olderSource)

    git('checkout', '--detach', installedSha)
    git('commit', '--allow-empty', '-qm', 'diverged local build')
    assert.equal(await reconcileDesktopBuild(status, {
      managedPackagedApp: true, installedSha: git('rev-parse', 'HEAD'), isAncestor
    }), status)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('only a successfully checked managed app is eligible for rebuild recovery', async () => {
  const currentSha = 'a'.repeat(40)
  const installedSha = 'b'.repeat(40)
  const status = { supported: true, updateAvailable: false, currentSha, targetSha: currentSha }

  const isAncestor = async () => { throw new Error('ancestry should not be queried') }

  for (const candidate of [
    { ...status, supported: false },
    { ...status, error: 'fetch-failed' },
    { ...status, updateAvailable: true },
    { ...status, targetSha: 'c'.repeat(40) }
  ]) {
    assert.equal(await reconcileDesktopBuild(candidate, {
      managedPackagedApp: true, installedSha, isAncestor
    }), candidate)
  }

  assert.equal(await reconcileDesktopBuild(status, {
    managedPackagedApp: false, installedSha, isAncestor
  }), status)
  assert.equal(await reconcileDesktopBuild(status, {
    managedPackagedApp: true, installedSha: null, isAncestor
  }), status)
})
