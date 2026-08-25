import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { electronBinaryCandidates, resolveElectronBinary } from '../e2e/electron-path'

const desktopRoot = path.join(path.sep, 'workspace', 'apps', 'desktop')
const repoRoot = path.join(path.sep, 'workspace')

test('Electron binary candidates cover clean package layouts on every platform', () => {
  assert.deepEqual(electronBinaryCandidates('darwin', desktopRoot, repoRoot), [
    path.join(desktopRoot, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron'),
    path.join(repoRoot, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
  ])
  assert.deepEqual(electronBinaryCandidates('win32', desktopRoot, repoRoot), [
    path.join(desktopRoot, 'node_modules', 'electron', 'dist', 'electron.exe'),
    path.join(repoRoot, 'node_modules', 'electron', 'dist', 'electron.exe')
  ])
  assert.deepEqual(electronBinaryCandidates('linux', desktopRoot, repoRoot), [
    path.join(desktopRoot, 'node_modules', 'electron', 'dist', 'electron'),
    path.join(repoRoot, 'node_modules', 'electron', 'dist', 'electron')
  ])
})

test('Electron resolver prefers the desktop package and uses the platform PATH command only as fallback', () => {
  const candidates = electronBinaryCandidates('darwin', desktopRoot, repoRoot)
  const lookups: string[] = []
  assert.equal(
    resolveElectronBinary({
      desktopRoot,
      exists: candidate => candidate === candidates[0],
      lookup: command => {
        lookups.push(command)
        return null
      },
      platform: 'darwin',
      repoRoot
    }),
    candidates[0]
  )
  assert.deepEqual(lookups, [])

  assert.equal(
    resolveElectronBinary({
      desktopRoot,
      exists: () => false,
      lookup: command => {
        lookups.push(command)
        return 'C:\\Electron\\electron.exe'
      },
      platform: 'win32',
      repoRoot
    }),
    'C:\\Electron\\electron.exe'
  )
  assert.equal(lookups.at(-1), 'where')
})
