import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'

const repoRoot = path.resolve(import.meta.dirname, '..')
const workflowPath = path.join(repoRoot, '.github', 'workflows', 'desktop-macos-package.yml')
const verifierPath = path.join(repoRoot, 'scripts', 'verify-desktop-dmg-gatekeeper.sh')

const expected = {
  runner: 'macos-15',
  buildCommand: 'npm run dist:mac:dmg --workspace apps/desktop'
}

test('workflow builds and exercises the current fresh arm64 DMG without Gatekeeper bypasses', () => {
  assert.ok(fs.existsSync(workflowPath), 'fresh macOS DMG workflow must exist')
  const workflow = fs.readFileSync(workflowPath, 'utf8')

  assert.match(workflow, /desktop-dmg-gatekeeper-contract\.test\.mjs/)
  assert.match(workflow, /workflow_dispatch:/)
  assert.match(workflow, /permissions:\s*\n\s+contents: read/)
  assert.match(workflow, new RegExp(`runs-on: ${expected.runner}`))
  assert.match(workflow, /timeout-minutes: 180/)
  assert.match(workflow, new RegExp(expected.buildCommand.replaceAll('.', '\\.')))
  assert.match(workflow, /Credentialed installed-app login/)
  assert.match(
    workflow,
    /ditto "\$mount\/Ansatz\.app" "\/Applications\/Ansatz\.app"/
  )
  assert.match(workflow, /desktop-credential-login\.mjs/)
  assert.match(workflow, /actions\/upload-artifact@[0-9a-f]{40}/)
  assert.match(workflow, /apps\/desktop\/release\/\*-mac-arm64\.dmg/)
  assert.match(workflow, /retention-days: 14/)
  assert.match(workflow, /if-no-files-found: error/)
  assert.doesNotMatch(workflow, /gh release download/, 'acceptance must build the current commit')

  for (const bypass of ['xattr -d', 'xattr -c', 'spctl --add', 'spctl --master-disable', '--no-quarantine']) {
    assert.ok(!workflow.includes(bypass), `Gatekeeper bypass is forbidden: ${bypass}`)
  }
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
  assert.match(verifier, /INSTALL_APP="\/Applications\/Ansatz\.app"/)
  assert.match(verifier, /Contents\/MacOS\/Ansatz/)
  assert.doesNotMatch(verifier, /Contents\/MacOS\/Hermes/)
  assert.match(verifier, /com\.apple\.quarantine/)
  assert.match(verifier, /cn\.c2sml\.ansatz\.voice-trace-client/)
  assert.doesNotMatch(verifier, /com\.nousresearch\.hermes/)
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
