import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { AUTH_FREE_CHANNELS, CHANNEL_AUTH_POLICY, createGuardedIpc, IpcAuthRequiredError } from './guarded-ipc'

class FakeIpcMain {
  readonly handles = new Map<string, (...args: any[]) => any>()
  readonly listeners = new Map<string, (...args: any[]) => any>()

  handle(channel: string, listener: (...args: any[]) => any) {
    this.handles.set(channel, listener)
  }

  on(channel: string, listener: (...args: any[]) => any) {
    this.listeners.set(channel, listener)

    return this
  }
}

function fixture(overrides: Record<string, unknown> = {}) {
  const ipcMain = new FakeIpcMain()

  const authority = {
    ownsSender: vi.fn(() => true),
    require: vi.fn(async () => {}),
    resolveConnectionId: vi.fn(({ policy, args }) => {
      if (policy === 'auth-free') {
        return null
      }

      if (policy === 'local') {
        return 'local'
      }

      return args[0]?.connectionId ?? null
    }),
    ...overrides
  }

  const guarded = createGuardedIpc(ipcMain as any, () => authority as any)

  return { authority, guarded, ipcMain }
}

const knownEvent = { sender: { id: 7 }, returnValue: undefined }

test('the single policy table contains the required account, local, connection, and combined cases', () => {
  assert.equal(CHANNEL_AUTH_POLICY['hermes:auth:status'], 'auth-free')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:auth:login'], 'auth-free')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:auth:logout'], 'auth-free')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:bootstrap:get'], 'auth-free')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:zoom:get'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:zoom:set-percent'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:titlebar-theme'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:native-theme'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:translucency'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:terminal:start'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:fs:writeText'], 'local')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:gateway:ws-url-for'], 'connection')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:connections:set-primary'], 'both')
  assert.equal(CHANNEL_AUTH_POLICY['hermes:connection-config:apply'], 'both')
  assert.deepEqual(
    [...AUTH_FREE_CHANNELS].sort(),
    Object.entries(CHANNEL_AUTH_POLICY)
      .filter(([, policy]) => policy === 'auth-free')
      .map(([channel]) => channel)
      .sort()
  )
})

test('an unclassified channel fails during registration before its handler can run', () => {
  const { guarded } = fixture()
  const handler = vi.fn()

  assert.throws(() => guarded.handle('hermes:unclassified', handler), /IPC channel is not classified/)
  assert.equal(handler.mock.calls.length, 0)
})

test('a protected handle resolves the execution connection and authorizes before invoking', async () => {
  const { authority, guarded, ipcMain } = fixture()
  const handler = vi.fn(async (_event, payload) => ({ id: payload.connectionId }))
  guarded.handle('hermes:gateway:ws-url-for', handler)

  assert.deepEqual(await ipcMain.handles.get('hermes:gateway:ws-url-for')?.(knownEvent, { connectionId: 'remote-a' }), {
    id: 'remote-a'
  })
  assert.deepEqual(authority.require.mock.calls, [['connection', 'remote-a']])
  assert.equal(handler.mock.calls.length, 1)
})

test('locked and stale authorities fail closed with one redacted error', async () => {
  for (const secret of ['agent_history_sessionid=do-not-leak', 'stale scope password-sentinel']) {
    const { guarded, ipcMain } = fixture({
      require: vi.fn(async () => {
        throw new Error(secret)
      })
    })

    const handler = vi.fn()
    guarded.handle('hermes:fs:writeText', handler)

    await assert.rejects(
      ipcMain.handles.get('hermes:fs:writeText')?.(knownEvent, '/tmp/a', 'content'),
      error =>
        error instanceof IpcAuthRequiredError && error.code === 'AUTH_REQUIRED' && !error.message.includes(secret)
    )
    assert.equal(handler.mock.calls.length, 0)
  }
})

test('unknown senders and missing connection ids never reach authority or handler', async () => {
  for (const overrides of [{ ownsSender: vi.fn(() => false) }, { resolveConnectionId: vi.fn(() => null) }]) {
    const { authority, guarded, ipcMain } = fixture(overrides)
    const handler = vi.fn()
    guarded.handle('hermes:gateway:ws-url-for', handler)

    await assert.rejects(
      ipcMain.handles.get('hermes:gateway:ws-url-for')?.(knownEvent, {}),
      error => error instanceof IpcAuthRequiredError && error.message === 'AUTH_REQUIRED'
    )
    assert.equal(handler.mock.calls.length, 0)

    if (overrides.ownsSender) {
      assert.equal(authority.require.mock.calls.length, 0)
    }
  }
})

test('an explicitly malformed connection id is rejected instead of falling back to a primary connection', async () => {
  const { authority, guarded, ipcMain } = fixture({
    resolveConnectionId: vi.fn(({ args }) => {
      const value = args[0]?.connectionId

      return typeof value === 'string' && !value.includes('\0') ? value : null
    })
  })

  const handler = vi.fn()
  guarded.handle('hermes:api', handler)

  await assert.rejects(
    ipcMain.handles.get('hermes:api')?.(knownEvent, { connectionId: 'remote-a\0remote-b', path: '/api/status' }),
    /AUTH_REQUIRED/
  )
  assert.equal(authority.require.mock.calls.length, 0)
  assert.equal(handler.mock.calls.length, 0)
})

test('send-style handlers are denied without executing their side effect', async () => {
  const { guarded, ipcMain } = fixture({
    require: vi.fn(async () => {
      throw new Error('private bridge failure')
    })
  })

  const handler = vi.fn()
  guarded.on('hermes:terminal:write', handler)

  ipcMain.listeners.get('hermes:terminal:write')?.(knownEvent, 'pty-1', 'whoami')
  await vi.waitFor(() => assert.deepEqual(knownEvent.returnValue, { error: { code: 'AUTH_REQUIRED' } }))
  assert.equal(handler.mock.calls.length, 0)
})

test('coverage is compared to table keys without a hard-coded handler count', () => {
  const { guarded } = fixture()

  for (const channel of Object.keys(CHANNEL_AUTH_POLICY)) {
    guarded.handle(channel, vi.fn())
  }

  assert.doesNotThrow(() => guarded.assertCoverage())

  const incomplete = fixture().guarded
  incomplete.handle('hermes:auth:status', vi.fn())
  assert.throws(() => incomplete.assertCoverage(), /Missing guarded IPC registrations/)
})
