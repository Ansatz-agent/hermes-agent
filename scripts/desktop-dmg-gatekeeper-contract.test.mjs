import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const repoRoot = path.resolve(import.meta.dirname, '..')
const workflowPath = path.join(repoRoot, '.github', 'workflows', 'desktop-dmg-gatekeeper.yml')
const verifierPath = path.join(repoRoot, 'scripts', 'verify-desktop-dmg-gatekeeper.sh')

const expected = {
  branch: 'integration/desktop-dmg-auth-e2e',
  runner: 'macos-26',
  asset: 'Hermes-0.17.0-mac-arm64.dmg',
  bytes: '166088977',
  sha256: '02f99b5e740a312d03b7c6da099016beed278003e51001a06950b72781f3c70f',
  tag: 'desktop-dmg-gatekeeper-02f99b5e'
}

test('workflow pins the private exact-DMG fresh-arm64 acceptance boundary', () => {
  assert.ok(fs.existsSync(workflowPath), 'fresh macOS DMG workflow must exist')
  const workflow = fs.readFileSync(workflowPath, 'utf8')

  assert.match(workflow, /branches:\s*\[integration\/desktop-dmg-auth-e2e\]/)
  assert.match(workflow, /paths:/)
  assert.match(workflow, /desktop-dmg-gatekeeper\.yml/)
  assert.match(workflow, /verify-desktop-dmg-gatekeeper\.sh/)
  assert.match(workflow, /desktop-dmg-gatekeeper-contract\.test\.mjs/)
  assert.match(workflow, /permissions:\s*\n\s+contents: read/)
  assert.match(workflow, new RegExp(`runs-on: ${expected.runner}`))
  assert.match(workflow, /timeout-minutes: 30/)
  assert.match(workflow, new RegExp(expected.tag))
  assert.match(workflow, new RegExp(expected.asset.replaceAll('.', '\\.')))
  assert.match(workflow, new RegExp(expected.sha256))
  assert.match(workflow, new RegExp(expected.bytes))
  assert.match(workflow, /gh release download/)
  assert.match(workflow, /GH_TOKEN: \$\{\{ github\.token \}\}/)
  assert.match(workflow, /if: always\(\)/)
  assert.match(workflow, /retention-days: 7/)
  assert.match(workflow, /if-no-files-found: error/)
  assert.match(workflow, /steps\.verify\.outcome/)
  assert.doesNotMatch(
    workflow,
    /path:\s*[^\n]*\.dmg/,
    'the DMG must not be copied into an Actions artifact'
  )
})

test('verifier rejects identity drift and never bypasses Gatekeeper', () => {
  assert.ok(fs.existsSync(verifierPath), 'fresh macOS DMG verifier must exist')
  const verifier = fs.readFileSync(verifierPath, 'utf8')

  assert.match(verifier, /uname -m/)
  assert.match(verifier, /arm64/)
  assert.match(verifier, /shasum -a 256/)
  assert.match(verifier, /stat -f '%z'/)
  assert.match(verifier, /hdiutil verify/)
  assert.match(verifier, /hdiutil attach -readonly -nobrowse/)
  assert.match(verifier, /Applications -> \/Applications/)
  assert.match(verifier, /INSTALL_APP="\/Applications\/Hermes\.app"/)
  assert.match(verifier, /com\.apple\.quarantine/)
  assert.match(verifier, /com\.nousresearch\.hermes/)
  assert.match(verifier, /CFBundleShortVersionString/)
  assert.match(verifier, /hermes-backend\.tar\.gz/)
  assert.match(verifier, /bootstrap\/install\.sh/)
  assert.match(verifier, /payload-manifest\.json/)
  assert.match(verifier, /install-stamp\.json/)
  assert.match(verifier, /plutil -convert xml1 -o \/dev\/null --/)
  assert.match(verifier, /codesign --verify --deep --strict/)
  assert.match(verifier, /spctl --assess --type open --context context:primary-signature/)
  assert.match(verifier, /spctl --assess --type execute/)
  assert.match(verifier, /open -n "\$INSTALL_APP"/)
  assert.match(verifier, /NOT_RUN/)
  assert.doesNotMatch(
    verifier,
    /-x "\$RESOURCES\/bootstrap\/install\.sh"/,
    'the app invokes the bundled installer through bash, so an executable bit is not required'
  )

  const hashCheck = verifier.indexOf('shasum -a 256')
  const sizeCheck = verifier.indexOf("stat -f '%z'")
  const attach = verifier.indexOf('hdiutil attach -readonly -nobrowse')
  assert.ok(hashCheck >= 0 && hashCheck < attach, 'hash must be checked before mounting')
  assert.ok(sizeCheck >= 0 && sizeCheck < attach, 'size must be checked before mounting')
  assert.ok(
    verifier.split('com.apple.quarantine').length >= 3,
    'quarantine must be applied to both the DMG and installed app'
  )

  for (const bypass of [
    'xattr -d',
    'xattr -c',
    'spctl --add',
    'spctl --master-disable',
    '--no-quarantine'
  ]) {
    assert.ok(!verifier.includes(bypass), `Gatekeeper bypass is forbidden: ${bypass}`)
  }
})
