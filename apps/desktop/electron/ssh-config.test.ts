import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  appendKnownHostKeyAtomic,
  collectSshConfigHosts,
  parseSshConfigHosts,
  parseSshConfigIncludes,
  parseSshGOutput
} from './ssh-config'

test('parseSshConfigHosts keeps literal aliases and drops wildcard/negated patterns', () => {
  const cfg = [
    'Host devbox',
    '  HostName 10.0.0.5',
    'Host *.internal prod !staging glob*',
    'Host alpha beta',
    '# Host commented-out',
    'host lower-case'
  ].join('\n')

  assert.deepEqual(parseSshConfigHosts(cfg), ['devbox', 'prod', 'alpha', 'beta', 'lower-case'])
})

test('parseSshConfigHosts de-duplicates', () => {
  assert.deepEqual(parseSshConfigHosts('Host box\nHost box\nHost box other'), ['box', 'other'])
})

test('parseSshConfigIncludes extracts include tokens', () => {
  const cfg = 'Include ~/.ssh/config.d/*\nInclude work_hosts personal_hosts\n# Include ignored'
  assert.deepEqual(parseSshConfigIncludes(cfg), ['~/.ssh/config.d/*', 'work_hosts', 'personal_hosts'])
})

test('collectSshConfigHosts follows Include directives (read-only)', () => {
  const files = {
    '/home/u/.ssh/config': 'Host main\nInclude work\nInclude ~/abs_inc',
    '/home/u/.ssh/work': 'Host work-box\nInclude nested',
    '/home/u/.ssh/nested': 'Host deep',
    '/home/u/abs_inc': 'Host home-abs'
  }

  const hosts = collectSshConfigHosts('/home/u/.ssh/config', {
    homeDir: '/home/u',
    readFile: p => files[p] ?? null
  })

  assert.deepEqual(hosts.sort(), ['deep', 'home-abs', 'main', 'work-box'].sort())
})

test('collectSshConfigHosts tolerates a missing config file', () => {
  assert.deepEqual(collectSshConfigHosts('/nope/config', { homeDir: '/home/u', readFile: () => null }), [])
})

test('collectSshConfigHosts does not loop on a self-include cycle', () => {
  const files = {
    '/home/u/.ssh/config': 'Host a\nInclude loop',
    '/home/u/.ssh/loop': 'Host b\nInclude config' // points back at config
  }

  const hosts = collectSshConfigHosts('/home/u/.ssh/config', {
    homeDir: '/home/u',
    readFile: p => files[p] ?? null
  })

  assert.deepEqual(hosts.sort(), ['a', 'b'])
})

test('collectSshConfigHosts expands globbed includes via injected globSync', () => {
  const files = {
    '/home/u/.ssh/config': 'Host root\nInclude config.d/*',
    '/home/u/.ssh/config.d/10-work': 'Host work',
    '/home/u/.ssh/config.d/20-home': 'Host home'
  }

  const hosts = collectSshConfigHosts('/home/u/.ssh/config', {
    homeDir: '/home/u',
    readFile: p => files[p] ?? null,
    globSync: pattern =>
      pattern.endsWith('config.d/*') ? ['/home/u/.ssh/config.d/10-work', '/home/u/.ssh/config.d/20-home'] : [pattern]
  })

  assert.deepEqual(hosts.sort(), ['home', 'root', 'work'].sort())
})

test('parseSshGOutput pulls hostname/user/port/identityfile', () => {
  const out = [
    'host devbox',
    'hostname 10.0.0.5',
    'user alice',
    'port 2222',
    'identityfile ~/.ssh/id_ed25519',
    'forwardagent no'
  ].join('\n')

  assert.deepEqual(parseSshGOutput(out), {
    hostname: '10.0.0.5',
    user: 'alice',
    port: 2222,
    identityFile: '~/.ssh/id_ed25519'
  })
})

test('parseSshGOutput takes the FIRST identityfile and tolerates missing keys', () => {
  const out = 'hostname box\nidentityfile ~/.ssh/a\nidentityfile ~/.ssh/b'
  const parsed = parseSshGOutput(out)
  assert.equal(parsed.identityFile, '~/.ssh/a')
  assert.equal(parsed.user, null)
  assert.equal(parsed.port, null)
})

test('appendKnownHostKeyAtomic preserves entries and replaces known_hosts with mode 0600', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-known-hosts-'))
  const sshDir = path.join(root, '.ssh')
  const knownHostsPath = path.join(sshDir, 'known_hosts')

  try {
    fs.mkdirSync(sshDir, { mode: 0o700 })
    fs.writeFileSync(knownHostsPath, 'old.example ssh-ed25519 b2xk\n', { mode: 0o644 })

    appendKnownHostKeyAtomic('new.example ssh-ed25519 bmV3', { homeDir: root })

    assert.equal(
      fs.readFileSync(knownHostsPath, 'utf8'),
      'old.example ssh-ed25519 b2xk\nnew.example ssh-ed25519 bmV3\n'
    )
    assert.equal(fs.statSync(knownHostsPath).mode & 0o777, 0o600)
    assert.deepEqual(
      fs.readdirSync(sshDir).filter(name => name !== 'known_hosts'),
      [],
      'same-directory temporary file must be renamed or removed'
    )
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

test('appendKnownHostKeyAtomic rejects multiline input without touching known_hosts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-known-hosts-'))
  const sshDir = path.join(root, '.ssh')
  const knownHostsPath = path.join(sshDir, 'known_hosts')

  try {
    fs.mkdirSync(sshDir, { mode: 0o700 })
    fs.writeFileSync(knownHostsPath, 'old.example ssh-ed25519 b2xk\n')
    assert.throws(
      () => appendKnownHostKeyAtomic('new.example ssh-ed25519 bmV3\nevil.example ssh-rsa ZXZpbA==', { homeDir: root }),
      /invalid ssh host key/i
    )
    assert.equal(fs.readFileSync(knownHostsPath, 'utf8'), 'old.example ssh-ed25519 b2xk\n')
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})
