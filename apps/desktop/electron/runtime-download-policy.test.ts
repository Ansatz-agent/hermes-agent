import assert from 'node:assert/strict'

import { test } from 'vitest'

import { buildManagedDownloadEnvironment } from './runtime-download-policy'

const HOSTILE = {
  HOME: '/Users/example',
  PATH: '/usr/bin:/bin',
  SYSTEMROOT: 'C:\\Windows',
  PIP_INDEX_URL: 'https://attacker.invalid/pypi',
  PIP_EXTRA_INDEX_URL: 'https://attacker.invalid/extra',
  PIP_CONFIG_FILE: '/tmp/attacker-pip.conf',
  UV_DEFAULT_INDEX: 'https://attacker.invalid/uv',
  UV_INDEX: 'https://attacker.invalid/uv-index',
  UV_CONFIG_FILE: '/tmp/attacker-uv.toml',
  npm_config_registry: 'https://attacker.invalid/npm',
  NPM_CONFIG_USERCONFIG: '/tmp/attacker-npmrc',
  NODEJS_ORG_MIRROR: 'https://attacker.invalid/node',
  ELECTRON_MIRROR: 'https://attacker.invalid/electron',
  ELECTRON_BUILDER_BINARIES_MIRROR: 'https://attacker.invalid/builder',
  PLAYWRIGHT_DOWNLOAD_HOST: 'https://attacker.invalid/playwright',
  HF_ENDPOINT: 'https://attacker.invalid/hf',
  HUGGING_FACE_HUB_TOKEN: 'secret-token',
  PYTHONPATH: '/tmp/injected',
  PYTHONHOME: '/tmp/injected-home'
}

test('managed download phases use registered domestic mirrors and strip hostile inherited redirects', () => {
  for (const phase of ['auth-payload-build', 'runtime-install', 'repair', 'update', 'lazy-feature'] as const) {
    const env = buildManagedDownloadEnvironment(HOSTILE, phase)

    assert.equal(env.HOME, HOSTILE.HOME)
    assert.equal(env.PATH, HOSTILE.PATH)
    assert.equal(env.SYSTEMROOT, HOSTILE.SYSTEMROOT)
    assert.equal(env.UV_DEFAULT_INDEX, 'https://mirrors.ustc.edu.cn/pypi/simple')
    assert.equal(env.UV_INDEX, 'https://mirrors.ustc.edu.cn/pypi/simple')
    assert.equal(env.PIP_INDEX_URL, 'https://mirrors.ustc.edu.cn/pypi/simple')
    assert.equal(env.HERMES_UV_FALLBACK_INDEX, 'https://pypi.tuna.tsinghua.edu.cn/simple')
    assert.equal(env.NPM_CONFIG_REGISTRY, 'https://registry.npmmirror.com')
    assert.equal(env.npm_config_registry, 'https://registry.npmmirror.com')
    assert.equal(env.NODEJS_ORG_MIRROR, 'https://registry.npmmirror.com/-/binary/node/')
    assert.equal(env.PLAYWRIGHT_DOWNLOAD_HOST, 'https://registry.npmmirror.com/-/binary/playwright')
    assert.equal(env.ELECTRON_MIRROR, 'https://npmmirror.com/mirrors/electron/')
    assert.equal(env.ELECTRON_BUILDER_BINARIES_MIRROR, 'https://npmmirror.com/mirrors/electron-builder-binaries/')
    assert.equal(env.PIP_EXTRA_INDEX_URL, undefined)
    assert.equal(env.UV_CONFIG_FILE, undefined)
    assert.equal(env.NPM_CONFIG_USERCONFIG, undefined)
    assert.equal(env.HUGGING_FACE_HUB_TOKEN, undefined)
    assert.equal(env.PYTHONPATH, undefined)
    assert.equal(env.PYTHONHOME, undefined)
    assert.equal(JSON.stringify(env).includes('attacker.invalid'), false)
    assert.equal(JSON.stringify(env).includes('secret-token'), false)
  }
})

test('managed environment is immutable between calls and rejects unknown phases', () => {
  const first = buildManagedDownloadEnvironment({}, 'runtime-install')
  first.UV_DEFAULT_INDEX = 'https://attacker.invalid/mutated'
  const second = buildManagedDownloadEnvironment({}, 'runtime-install')
  assert.equal(second.UV_DEFAULT_INDEX, 'https://mirrors.ustc.edu.cn/pypi/simple')
  assert.throws(
    () => buildManagedDownloadEnvironment({}, 'unknown' as never),
    /unknown managed download phase/
  )
})
