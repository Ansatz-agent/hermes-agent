import assert from 'node:assert/strict'
import test from 'node:test'

import {
  WINDOWS_PACKAGE_ENVIRONMENT,
  packageWindowsCommands,
  validateWindowsPackageHost
} from './package-desktop-windows.mjs'

test('Windows package host accepts the pinned x64 toolchain', () => {
  assert.doesNotThrow(() =>
    validateWindowsPackageHost({
      platform: 'win32',
      arch: 'x64',
      nodeVersion: 'v26.7.0'
    })
  )
})

test('Windows package host rejects unsupported platform, architecture, and Node versions', () => {
  assert.throws(
    () => validateWindowsPackageHost({ platform: 'darwin', arch: 'arm64', nodeVersion: 'v26.7.0' }),
    /requires win32-x64/
  )
  assert.throws(
    () => validateWindowsPackageHost({ platform: 'win32', arch: 'arm64', nodeVersion: 'v26.7.0' }),
    /requires win32-x64/
  )
  assert.throws(
    () => validateWindowsPackageHost({ platform: 'win32', arch: 'x64', nodeVersion: 'v26.6.0' }),
    /requires Node v26\.7\.0/
  )
})

test('package command plan installs, verifies, builds, and smoke-tests in order', () => {
  const steps = packageWindowsCommands('npm.cmd')
  assert.deepEqual(
    steps.map(step => step.args),
    [
      ['ci'],
      ['run', 'test:desktop:windows-contract'],
      ['run', 'typecheck', '--workspace', 'apps/desktop'],
      ['run', 'dist:win:nsis', '--workspace', 'apps/desktop'],
      ['run', 'test:desktop:nsis', '--workspace', 'apps/desktop']
    ]
  )
  assert.equal(steps.at(-1).name, 'validate unpacked desktop bundle')
})

test('package environment keeps browser downloads disabled and mirrors explicit', () => {
  assert.equal(WINDOWS_PACKAGE_ENVIRONMENT.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD, '1')
  assert.equal(WINDOWS_PACKAGE_ENVIRONMENT.CSC_IDENTITY_AUTO_DISCOVERY, 'false')
  assert.match(WINDOWS_PACKAGE_ENVIRONMENT.ELECTRON_MIRROR, /^https:\/\//)
  assert.match(WINDOWS_PACKAGE_ENVIRONMENT.ELECTRON_BUILDER_BINARIES_MIRROR, /^https:\/\//)
})
