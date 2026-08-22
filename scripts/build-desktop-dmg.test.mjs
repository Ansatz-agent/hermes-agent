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

function runPipeline({
  produceDmg,
  builderBinariesMirror,
  signatureValid = true,
  builderVolumeDenied = false,
  prepareAuthInputs = false,
  lockCurrent = true
}) {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dmg-pipeline-'))
  const releaseDir = path.join(tempRoot, 'release')
  const packagedApp = path.join(releaseDir, 'mac-arm64', 'Hermes.app')
  const fakeNode = path.join(tempRoot, 'node')
  const fakeNpm = path.join(tempRoot, 'npm')
  const fakeCodesign = path.join(tempRoot, 'codesign')
  const fakeHdiutil = path.join(tempRoot, 'hdiutil')
  const fakeUv = path.join(tempRoot, 'uv')
  const recordPath = path.join(tempRoot, 'npm-record.txt')
  const authInputDir = path.join(tempRoot, 'auth-inputs')
  const authOutputDir = path.join(tempRoot, 'auth-output')
  const prepareScript = path.join(
    repoRoot,
    'apps',
    'desktop',
    'scripts',
    'prepare-auth-toolchain-inputs.mjs'
  )
  const artifactPath = path.join(
    releaseDir,
    `Hermes-test-${process.pid}-${Date.now()}-mac-arm64.dmg`
  )

  fs.writeFileSync(
    fakeNode,
    `#!/bin/sh
if [ "\${1:-}" = "--version" ]; then
  printf '%s\\n' 'v26.7.0'
  exit 0
fi
if [ "\${HERMES_DMG_TEST_PREPARE_AUTH:-0}" = "1" ] && [ "\${1:-}" = "$HERMES_DMG_TEST_PREPARE_SCRIPT" ]; then
  printf '%s\\n' 'prepare-auth-toolchain' >> "$HERMES_DMG_TEST_RECORD"
  mkdir -p "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/wheelhouse"
  cp "$HERMES_DMG_TEST_FAKE_UV" "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/uv"
  printf '%s\\n' 'python' > "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/python.tar.gz"
  printf '%s\\n' 'httpx==0.28.1 --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' > "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/auth-requirements.txt"
  printf '%s\\n' 'wheel' > "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/wheelhouse/httpx.whl"
  printf '%s\\n' '{"uvVersion":"0.12.5","pythonVersion":"3.11.16"}' > "$HERMES_AUTH_TOOLCHAIN_INPUT_DIR/metadata.json"
  exit 0
fi
exec '${process.execPath}' "$@"
`,
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeUv,
    '#!/bin/sh\nprintf \'uv-lock-check|%s\\n\' "$*" >> "$HERMES_DMG_TEST_RECORD"\n[ "$HERMES_DMG_TEST_LOCK_CURRENT" = "1" ]\n',
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeNpm,
    `#!/bin/sh\nprintf '%s|%s|%s|%s\\n' "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" "$ELECTRON_MIRROR" "$ELECTRON_BUILDER_BINARIES_MIRROR" "$*" >> "$HERMES_DMG_TEST_RECORD"\nif [ "\${1:-}" = "run" ]; then\n  mkdir -p "$HERMES_DMG_TEST_PACKAGED_APP"\nfi\nif [ "\${HERMES_DMG_TEST_VOLUME_DENIED:-0}" = "1" ] && [ "\${1:-}" = "run" ]; then\n  printf '%s\\n' 'ditto: /Volumes/Install Hermes/Hermes.app: Operation not permitted' >&2\n  exit 1\nfi\nif [ "\${HERMES_DMG_TEST_PRODUCE:-0}" = "1" ] && [ "\${1:-}" = "run" ]; then\n  mkdir -p "$(dirname "$HERMES_DMG_TEST_ARTIFACT")"\n  printf '%s\\n' 'fake dmg' > "$HERMES_DMG_TEST_ARTIFACT"\nfi\n`,
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeCodesign,
    `#!/bin/sh\nprintf 'codesign|%s\\n' "$*" >> "$HERMES_DMG_TEST_RECORD"\n[ "$HERMES_DMG_TEST_SIGNATURE_VALID" = "1" ]\n`,
    { mode: 0o755 }
  )
  fs.writeFileSync(
    fakeHdiutil,
    `#!/bin/sh\nprintf 'hdiutil|%s\\n' "$*" >> "$HERMES_DMG_TEST_RECORD"\ncase "\${1:-}" in\n  create|convert)\n    for argument in "$@"; do output="$argument"; done\n    mkdir -p "$(dirname "$output")"\n    printf '%s\\n' 'fallback dmg' > "$output"\n    ;;\n  detach)\n    for argument in "$@"; do mount_point="$argument"; done\n    [ -d "$mount_point/Hermes.app" ]\n    [ "$(readlink "$mount_point/Applications")" = "/Applications" ]\n    printf '%s\\n' 'fallback-contents|Hermes.app|Applications->/Applications' >> "$HERMES_DMG_TEST_RECORD"\n    ;;\nesac\n`,
    { mode: 0o755 }
  )

  fs.mkdirSync(path.join(authInputDir, 'wheelhouse'), { recursive: true })
  fs.copyFileSync(fakeUv, path.join(authInputDir, 'uv'))
  fs.chmodSync(path.join(authInputDir, 'uv'), 0o755)
  for (const file of ['python.tar.gz', 'auth-requirements.txt']) {
    fs.writeFileSync(path.join(authInputDir, file), `${file}\n`)
  }
  fs.writeFileSync(path.join(authInputDir, 'wheelhouse', 'httpx.whl'), 'wheel\n')

  try {
    const result = spawnSync('/bin/bash', [buildScript], {
      cwd: repoRoot,
      env: {
        ...process.env,
        PATH: `${tempRoot}:/usr/bin:/bin`,
        HERMES_DMG_TEST_RECORD: recordPath,
        HERMES_DMG_TEST_ARTIFACT: artifactPath,
        HERMES_DMG_RELEASE_DIR: releaseDir,
        HERMES_DMG_TEST_PACKAGED_APP: packagedApp,
        HERMES_DMG_TEST_PRODUCE: produceDmg ? '1' : '0',
        HERMES_DMG_TEST_VOLUME_DENIED: builderVolumeDenied ? '1' : '0',
        HERMES_DMG_TEST_SIGNATURE_VALID: signatureValid ? '1' : '0',
        HERMES_DMG_TEST_PREPARE_AUTH: prepareAuthInputs ? '1' : '0',
        HERMES_DMG_TEST_LOCK_CURRENT: lockCurrent ? '1' : '0',
        HERMES_DMG_TEST_FAKE_UV: fakeUv,
        HERMES_DMG_TEST_PREPARE_SCRIPT: prepareScript,
        HERMES_AUTH_TOOLCHAIN_INPUT_DIR: authInputDir,
        HERMES_AUTH_TOOLCHAIN_OUTPUT_DIR: authOutputDir,
        ...(!prepareAuthInputs
          ? {
              HERMES_AUTH_TOOLCHAIN_UV_PATH: path.join(authInputDir, 'uv'),
              HERMES_AUTH_TOOLCHAIN_PYTHON_ARCHIVE: path.join(authInputDir, 'python.tar.gz'),
              HERMES_AUTH_TOOLCHAIN_REQUIREMENTS: path.join(authInputDir, 'auth-requirements.txt'),
              HERMES_AUTH_TOOLCHAIN_WHEELHOUSE: path.join(authInputDir, 'wheelhouse'),
              HERMES_AUTH_TOOLCHAIN_UV_VERSION: '0.12.5',
              HERMES_AUTH_TOOLCHAIN_PYTHON_VERSION: '3.11.16'
            }
          : {}),
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
  assert.match(record, /uv-lock-check\|lock --check --config-file .+\/uv\.toml/)
  assert.match(record, /codesign\|--verify --deep --strict .+\/release\/mac-arm64\/Hermes\.app/)
  assert.match(result.stdout, /Hermes-test-.+-mac-arm64\.dmg/)
})

test('pipeline fails before npm when the signed runtime lock is stale', () => {
  const { result, record } = runPipeline({ produceDmg: true, lockCurrent: false })

  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /uv\.lock is not current/)
  assert.match(record, /uv-lock-check\|lock --check --config-file .+\/uv\.toml/)
  assert.doesNotMatch(record, /\|ci$/m)
})

test('pipeline prepares verified authentication toolchain inputs when they are not prebuilt', () => {
  const { result, record } = runPipeline({ produceDmg: true, prepareAuthInputs: true })

  assert.equal(result.status, 0, result.stderr)
  assert.match(record, /prepare-auth-toolchain/)
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

test('pipeline falls back to hdiutil only when dmgbuild cannot write its mounted volume', () => {
  const { result, record } = runPipeline({
    produceDmg: false,
    builderVolumeDenied: true
  })

  assert.equal(result.status, 0, result.stderr)
  assert.match(record, /hdiutil\|create -size \d+m -fs HFS\+ -volname Install Hermes -type UDIF /)
  assert.match(record, /hdiutil\|attach -nobrowse -mountpoint (?!\/Volumes\/)/)
  assert.match(record, /fallback-contents\|Hermes\.app\|Applications->\/Applications/)
  assert.match(record, /hdiutil\|convert .* -format UDZO -ov -o .*Hermes-0\.17\.0-mac-arm64\.dmg/)
  assert.match(record, /hdiutil\|verify .*Hermes-0\.17\.0-mac-arm64\.dmg/)
  assert.match(result.stdout, /restricted-volume fallback/)
  assert.match(result.stdout, /Hermes-0\.17\.0-mac-arm64\.dmg/)
})
