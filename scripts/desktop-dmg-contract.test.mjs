import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  findNewestDmg,
  forbiddenBrowserDownloadLine
} from './desktop-dmg-contract.mjs'

test('browser download validator rejects Playwright-managed browser downloads', () => {
  const forbiddenLines = [
    'Downloading Chromium 145.0.7632.6 (playwright build v1208)',
    'Downloading Chrome for Testing 145.0.7632.6',
    'Downloading FFMPEG playwright build v1011',
    '> playwright install chromium'
  ]

  for (const line of forbiddenLines) {
    assert.equal(forbiddenBrowserDownloadLine(`before\n${line}\nafter`), line)
  }
})

test('browser download validator accepts ordinary Electron build output', () => {
  const log = [
    'npm ci',
    'downloading electron-v40.10.2-darwin-arm64.zip',
    'building block map',
    'Ansatz-0.17.0-mac-arm64.dmg'
  ].join('\n')

  assert.equal(forbiddenBrowserDownloadLine(log), null)
})

test('findNewestDmg returns the newest macOS arm64 Ansatz artifact', () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-release-'))
  try {
    const older = path.join(releaseDir, 'Ansatz-0.16.0-mac-arm64.dmg')
    const newer = path.join(releaseDir, 'Ansatz-0.17.0-mac-arm64.dmg')
    fs.writeFileSync(older, 'older')
    fs.writeFileSync(newer, 'newer')
    fs.writeFileSync(path.join(releaseDir, 'Ansatz-0.17.0-mac-x64.dmg'), 'wrong arch')
    fs.utimesSync(older, new Date(1_000), new Date(1_000))
    fs.utimesSync(newer, new Date(2_000), new Date(2_000))

    assert.equal(findNewestDmg(releaseDir), newer)
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true })
  }
})

test('findNewestDmg fails when the build produced no matching artifact', () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-release-'))
  try {
    assert.throws(() => findNewestDmg(releaseDir), /no macOS arm64 Ansatz DMG found/)
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true })
  }
})

test('findNewestDmg ignores artifacts older than the current build', () => {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-release-'))
  try {
    const stale = path.join(releaseDir, 'Ansatz-0.17.0-mac-arm64.dmg')
    fs.writeFileSync(stale, 'stale')
    fs.utimesSync(stale, new Date(1_000), new Date(1_000))

    assert.throws(
      () => findNewestDmg(releaseDir, 2_000),
      /no macOS arm64 Ansatz DMG found/
    )
  } finally {
    fs.rmSync(releaseDir, { recursive: true, force: true })
  }
})
