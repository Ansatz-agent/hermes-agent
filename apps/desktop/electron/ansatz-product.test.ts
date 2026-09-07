import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  ANSATZ_PRODUCT,
  ansatzAuthEnvironment,
  buildAnsatzTerminalEnvironment,
  buildBundledRuntimeValidationEnvironment,
  resolveAnsatzCliPath,
  resolveAnsatzDesktopRuntimeRoot,
  resolveAnsatzRuntimeRoot,
  resolveAnsatzSshControlDirectory
} from './ansatz-product'

test('canonical CLI path wins and the legacy path is only a compatibility fallback', () => {
  const checked: string[] = []

  const exists = (candidate: string) => {
    checked.push(candidate)

    return candidate.endsWith('/ansatz')
  }

  assert.equal(resolveAnsatzCliPath('/bin/ansatz', '/bin/hermes', exists), '/bin/ansatz')
  assert.deepEqual(checked, ['/bin/ansatz'])
  assert.equal(
    resolveAnsatzCliPath('/missing/ansatz', '/bin/hermes', () => false),
    '/bin/hermes'
  )
})

test('Ansatz desktop identity cannot collide with an existing Hermes installation', () => {
  assert.equal(ANSATZ_PRODUCT.productName, 'Ansatz')
  assert.equal(ANSATZ_PRODUCT.appId, 'cn.c2sml.ansatz.voice-trace-client')
  assert.equal(ANSATZ_PRODUCT.executableName, 'Ansatz')
  assert.equal(ANSATZ_PRODUCT.protocolScheme, 'ansatz-voice-trace')
  assert.equal(ANSATZ_PRODUCT.authRuntimeNamespace, 'ansatz-voice-trace-client-auth-v2')
  assert.equal(ANSATZ_PRODUCT.authKeyringService, 'cn.c2sml.ansatz.voice-trace-client.remote-auth')
  assert.equal(ANSATZ_PRODUCT.legacyAuthKeyringService, 'cn.c2sml.hermes.remote-auth')
  assert.equal(ANSATZ_PRODUCT.mediaProtocol, 'ansatz-media')
  assert.equal(ANSATZ_PRODUCT.embedSessionPartition, 'persist:ansatz-voice-trace-embed')
  assert.equal(ANSATZ_PRODUCT.previewSessionPartition, 'persist:ansatz-voice-trace-preview')
  assert.equal(ANSATZ_PRODUCT.remoteOauthSessionPartition, 'persist:ansatz-voice-trace-remote-oauth')
  assert.equal(ANSATZ_PRODUCT.linkTitleSession, 'ansatz:link-titles')
  assert.equal(ANSATZ_PRODUCT.desktopProduct, 'ansatz-voice-trace')
  assert.equal(ANSATZ_PRODUCT.runtimeHomeOverrideEnvironmentVariable, 'ANSATZ_VOICE_TRACE_CLIENT_HOME')
  assert.deepEqual(ANSATZ_PRODUCT.posixLaunchers, [
    'ansatz-voice-trace',
    'ansatz-voice-trace-agent',
    'ansatz-voice-trace-acp'
  ])
  assert.deepEqual(ANSATZ_PRODUCT.canonicalCliLaunchers, ['ansatz', 'ansatz-agent', 'ansatz-acp'])
  assert.deepEqual(ANSATZ_PRODUCT.legacyCliLaunchers, ['hermes', 'hermes-agent', 'hermes-acp'])
})

test('desktop auth rolls to a new owner namespace without moving Keychain credentials', () => {
  assert.equal(ANSATZ_PRODUCT.authRuntimeNamespace, 'ansatz-voice-trace-client-auth-v2')
  assert.equal(ANSATZ_PRODUCT.authKeyringService, 'cn.c2sml.ansatz.voice-trace-client.remote-auth')
})

test('resolveAnsatzRuntimeRoot isolates macOS and Windows user data from Hermes', () => {
  assert.equal(resolveAnsatzRuntimeRoot('darwin', '/Users/a', ''), '/Users/a/.ansatz-voice-trace-client')
  assert.equal(
    resolveAnsatzRuntimeRoot('win32', 'C:\\Users\\a', 'C:\\Users\\a\\AppData\\Local'),
    'C:\\Users\\a\\AppData\\Local\\AnsatzVoiceTraceClient'
  )
  assert.notEqual(resolveAnsatzRuntimeRoot('darwin', '/Users/a', ''), '/Users/a/.hermes')
})

test('resolveAnsatzRuntimeRoot requires LOCALAPPDATA on Windows', () => {
  assert.throws(() => resolveAnsatzRuntimeRoot('win32', 'C:\\Users\\a', ''), /LOCALAPPDATA/)
})

test('packaged Ansatz desktop ignores an inherited generic HERMES_HOME', () => {
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: true,
      platform: 'darwin',
      homeDirectory: '/Users/a',
      inheritedHermesHome: '/Users/a/.hermes'
    }),
    '/Users/a/.ansatz-voice-trace-client'
  )
})

test('packaged Ansatz desktop accepts only its dedicated runtime-home override', () => {
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: true,
      platform: 'darwin',
      homeDirectory: '/Users/a',
      inheritedHermesHome: '/Users/a/.hermes',
      dedicatedRuntimeHomeOverride: '/tmp/ansatz-runtime',
      desktopUserDataOverride: '/tmp/different-electron-user-data'
    }),
    '/tmp/ansatz-runtime'
  )
})

test('desktop runtime overrides use the selected platform path semantics', () => {
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: true,
      platform: 'win32',
      homeDirectory: 'C:\\Users\\a',
      localAppData: 'C:\\Users\\a\\AppData\\Local',
      dedicatedRuntimeHomeOverride: 'D:\\sandbox\\..\\ansatz-runtime'
    }),
    'D:\\ansatz-runtime'
  )
})

test('desktop user-data isolation uses the selected platform path semantics', () => {
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: true,
      platform: 'win32',
      homeDirectory: 'C:\\Users\\a',
      localAppData: 'C:\\Users\\a\\AppData\\Local',
      desktopUserDataOverride: 'D:\\electron-user-data'
    }),
    'D:\\electron-user-data\\ansatz-voice-trace-home'
  )
})

test('desktop runtime selection preserves explicit development and test isolation', () => {
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: false,
      platform: 'darwin',
      homeDirectory: '/Users/a',
      inheritedHermesHome: '/Users/a/.hermes/profiles/test',
      normalizeInheritedRoot: root => root.replace('/profiles/test', '')
    }),
    '/Users/a/.hermes'
  )
  assert.equal(
    resolveAnsatzDesktopRuntimeRoot({
      isPackaged: true,
      platform: 'darwin',
      homeDirectory: '/Users/a',
      inheritedHermesHome: '/Users/a/.hermes',
      desktopUserDataOverride: '/tmp/ansatz-desktop-test'
    }),
    '/tmp/ansatz-desktop-test/ansatz-voice-trace-home'
  )
})

test('ansatzAuthEnvironment carries local auth identity without changing source values', () => {
  const source = {
    PATH: '/usr/bin',
    PROVIDER_API_KEY: 'filtered-by-auth-bridge',
    HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'must-not-trigger-automatic-keychain-access'
  }

  assert.deepEqual(ansatzAuthEnvironment('/Users/a/.ansatz-voice-trace-client', source), {
    PATH: '/usr/bin',
    PROVIDER_API_KEY: 'filtered-by-auth-bridge',
    HERMES_HOME: '/Users/a/.ansatz-voice-trace-client',
    HERMES_AUTH_RUNTIME_NAMESPACE: 'ansatz-voice-trace-client-auth-v2',
    HERMES_AUTH_KEYRING_SERVICE: 'cn.c2sml.ansatz.voice-trace-client.remote-auth'
  })
  assert.deepEqual(source, {
    PATH: '/usr/bin',
    PROVIDER_API_KEY: 'filtered-by-auth-bridge',
    HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'must-not-trigger-automatic-keychain-access'
  })
})

test('bundled runtime validation overrides ambient legacy state with the selected runtime home', () => {
  const source = {
    HERMES_AUTH_KEYRING_SERVICE: 'stale.remote-auth',
    HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'legacy.remote-auth',
    HERMES_AUTH_RUNTIME_NAMESPACE: 'stale-auth-v0',
    HERMES_HOME: '/Users/a/.hermes',
    PATH: '/usr/bin',
    PYTHONPATH: '/ambient/python'
  }

  assert.deepEqual(
    buildBundledRuntimeValidationEnvironment(
      '/Users/a/.ansatz-voice-trace-client/hermes-agent',
      '/Users/a/.ansatz-voice-trace-client',
      source
    ),
    {
      HERMES_AUTH_KEYRING_SERVICE: ANSATZ_PRODUCT.authKeyringService,
      HERMES_AUTH_RUNTIME_NAMESPACE: ANSATZ_PRODUCT.authRuntimeNamespace,
      HERMES_HOME: '/Users/a/.ansatz-voice-trace-client',
      PATH: '/usr/bin',
      PYTHONPATH: ['/Users/a/.ansatz-voice-trace-client/hermes-agent', '/ambient/python'].join(path.delimiter)
    }
  )
  assert.equal(source.HERMES_HOME, '/Users/a/.hermes')
})

test('embedded terminal environment pins the selected home and canonical Ansatz identity', () => {
  const result = buildAnsatzTerminalEnvironment('/Users/a/.ansatz-voice-trace-client', '0.17.0', {
    HERMES_AUTH_KEYRING_SERVICE: 'stale.remote-auth',
    HERMES_AUTH_LEGACY_KEYRING_SERVICE: 'legacy.remote-auth',
    HERMES_AUTH_RUNTIME_NAMESPACE: 'stale-auth-v0',
    HERMES_HOME: '/Users/a/.hermes',
    LC_CTYPE: 'zh_CN.UTF-8',
    NO_COLOR: '1',
    FORCE_COLOR: '0',
    COLORFGBG: '0;15',
    npm_config_prefix: '/tmp/npm',
    npm_package_name: 'desktop'
  })

  assert.equal(result.HERMES_HOME, '/Users/a/.ansatz-voice-trace-client')
  assert.equal(result.HERMES_AUTH_RUNTIME_NAMESPACE, ANSATZ_PRODUCT.authRuntimeNamespace)
  assert.equal(result.HERMES_AUTH_KEYRING_SERVICE, ANSATZ_PRODUCT.authKeyringService)
  assert.equal(result.HERMES_AUTH_LEGACY_KEYRING_SERVICE, undefined)
  assert.equal(result.TERM_PROGRAM, ANSATZ_PRODUCT.executableName)
  assert.equal(result.TERM_PROGRAM_VERSION, '0.17.0')
  assert.equal(result.TERM, 'xterm-256color')
  assert.equal(result.COLORTERM, 'truecolor')
  assert.equal(result.LC_CTYPE, 'zh_CN.UTF-8')
  assert.equal(result.HERMES_DESKTOP_TERMINAL, '1')
  assert.equal(result.NO_COLOR, undefined)
  assert.equal(result.FORCE_COLOR, undefined)
  assert.equal(result.COLORFGBG, undefined)
  assert.equal(result.npm_config_prefix, undefined)
  assert.equal(result.npm_package_name, undefined)
})

test('desktop SSH control sockets follow the selected runtime home', () => {
  assert.equal(
    resolveAnsatzSshControlDirectory('/Users/a/.ansatz-voice-trace-client'),
    path.join('/Users/a/.ansatz-voice-trace-client', 'desktop-ssh')
  )
})

function worstCaseOpenSshSocketPath(controlDirectory: string) {
  return path.posix.join(controlDirectory, `${'0'.repeat(16)}.sock`) + `.${'x'.repeat(16)}`
}

test.skipIf(process.platform === 'win32')(
  'long POSIX runtime homes use a short per-user isolated SSH directory',
  () => {
    const firstHome = `/Users/${'a'.repeat(120)}/.ansatz-voice-trace-client`
    const secondHome = `/Users/${'b'.repeat(120)}/.ansatz-voice-trace-client`
    const first = resolveAnsatzSshControlDirectory(firstHome)
    const second = resolveAnsatzSshControlDirectory(secondHome)
    const uid = process.getuid!()

    assert.match(first, new RegExp(`^/tmp/ansatz-vtc-ssh-${uid}-[0-9a-f]{16}$`))
    assert.match(second, new RegExp(`^/tmp/ansatz-vtc-ssh-${uid}-[0-9a-f]{16}$`))
    assert.notEqual(first, second)
    assert.ok(Buffer.byteLength(worstCaseOpenSshSocketPath(first)) < 104)
    assert.ok(Buffer.byteLength(worstCaseOpenSshSocketPath(second)) < 104)
  }
)

test.skipIf(process.platform === 'win32')('POSIX SSH path budgeting counts Unicode bytes, not characters', () => {
  const unicodeHome = `/Users/${'用户'.repeat(30)}/.ansatz-voice-trace-client`
  const controlDirectory = resolveAnsatzSshControlDirectory(unicodeHome)

  assert.notEqual(controlDirectory, path.join(unicodeHome, 'desktop-ssh'))
  assert.ok(Buffer.byteLength(worstCaseOpenSshSocketPath(controlDirectory)) < 104)
})
