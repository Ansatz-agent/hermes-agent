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

function runPipeline({ produceDmg, builderBinariesMirror, signatureValid = true }) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-pipeline-'))
  const fakeNode = path.join(tempRoot, 'node')
  const fakeNpm = path.join(tempRoot, 'npm')
  const fakeCodesign = path.join(tempRoot, 'codesign')
  const recordPath = path.join(tempRoot, 'npm-record.txt')
  const artifactPath = path.join(
    repoRoot,
    'apps',
    'desktop',
    'release',
    `Hermes-test-${process.pid}-${Date.now()}-mac-arm64.dmg`
  )

  fs.writeFileSync(
    fakeNode,
    `#!/bin/sh\nif [ "\${1:-}" = "--version" ]; then\n  printf '%s\\n' 'v26.7.0'\n  exit 0\nfi\nexec '${process.execPath}' "$@"\n`,
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeNpm,
    `#!/bin/sh\nprintf '%s|%s|%s|%s\\n' "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" "$ELECTRON_MIRROR" "$ELECTRON_BUILDER_BINARIES_MIRROR" "$*" >> "$HERMES_DMG_TEST_RECORD"\nif [ "\${HERMES_DMG_TEST_PRODUCE:-0}" = "1" ] && [ "\${1:-}" = "run" ]; then\n  mkdir -p "$(dirname "$HERMES_DMG_TEST_ARTIFACT")"\n  printf '%s\\n' 'fake dmg' > "$HERMES_DMG_TEST_ARTIFACT"\nfi\n`,
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeCodesign,
    `#!/bin/sh\nprintf 'codesign|%s\\n' "$*" >> "$HERMES_DMG_TEST_RECORD"\n[ "$HERMES_DMG_TEST_SIGNATURE_VALID" = "1" ]\n`,
    { mode: 0o755 }
  )

  try {
    const result = spawnSync('/bin/bash', [buildScript], {
      cwd: repoRoot,
      env: {
        ...process.env,
        PATH: `${tempRoot}:/usr/bin:/bin`,
        HERMES_DMG_TEST_RECORD: recordPath,
        HERMES_DMG_TEST_ARTIFACT: artifactPath,
        HERMES_DMG_TEST_PRODUCE: produceDmg ? '1' : '0',
        HERMES_DMG_TEST_SIGNATURE_VALID: signatureValid ? '1' : '0',
        ...(builderBinariesMirror
          ? { ELECTRON_BUILDER_BINARIES_MIRROR: builderBinariesMirror }
          : {})
      },
      encoding: 'utf8'
    })
    const record = fs.existsSync(recordPath) ? fs.readFileSync(recordPath, 'utf8') : ''
    return { result, record }
  } finally {
    fs.rmSync(artifactPath, { force: true })
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
  assert.match(
    result.stdout,
    /ELECTRON_BUILDER_BINARIES_MIRROR=https:\/\/npmmirror\.com\/mirrors\/electron-builder-binaries\//
  )
})

test('pipeline installs locked dependencies and builds the desktop DMG', () => {
  const { result, record } = runPipeline({ produceDmg: true })
  assert.equal(result.status, 0, result.stderr)
  assert.match(
    record,
    /1\|https:\/\/npmmirror\.com\/mirrors\/electron\/\|https:\/\/npmmirror\.com\/mirrors\/electron-builder-binaries\/\|ci/
  )
  assert.match(
    record,
    /1\|https:\/\/npmmirror\.com\/mirrors\/electron\/\|https:\/\/npmmirror\.com\/mirrors\/electron-builder-binaries\/\|run --workspace apps\/desktop dist:mac:dmg -- --config\.mac\.identity=-/
  )
  assert.match(record, /codesign\|--verify --deep --strict .+\/release\/mac-arm64\/Hermes\.app/)
  assert.match(result.stdout, /Hermes-test-.+-mac-arm64\.dmg/)
})

test('pipeline preserves a caller-provided builder binaries mirror', () => {
  const customMirror = 'https://downloads.example.test/electron-builder/'
  const { result, record } = runPipeline({
    produceDmg: true,
    builderBinariesMirror: customMirror
  })
  assert.equal(result.status, 0, result.stderr)
  assert.match(record, /\|https:\/\/downloads\.example\.test\/electron-builder\/\|ci/)
})

test('pipeline fails when the current build produces no DMG', () => {
  const { result } = runPipeline({ produceDmg: false })
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /no macOS arm64 Hermes DMG found/)
})

test('pipeline fails when the packaged app signature is invalid', () => {
  const { result } = runPipeline({ produceDmg: true, signatureValid: false })
  assert.notEqual(result.status, 0)
})
