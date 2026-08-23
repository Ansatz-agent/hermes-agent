import { describe, expect, it, vi } from 'vitest'

import {
  applyConnectionChange,
  applyForegroundConnectionChange,
  commitConnectionFailure,
  resolveTerminalConnection
} from './connection-apply'

function deferred() {
  let resolve!: () => void

  const promise = new Promise<void>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('applyConnectionChange', () => {
  it.each([['SSH A to SSH B'], ['SSH to Cloud'], ['Cloud to SSH']])(
    'serializes %s behind bootstrap rollback before teardown and apply',
    async () => {
      const gate = deferred()
      const events: string[] = []

      const run = applyConnectionChange({
        cancelAndWait: async () => {
          events.push('cancel')
          await gate.promise
          events.push('drained')
        },
        isPrimary: true,
        scope: '',
        sendApplied: () => events.push('applied'),
        stopPool: vi.fn(),
        teardownPrimary: async () => {
          events.push('primary')
        },
        teardownSsh: async () => {
          events.push('ssh')
        }
      })

      await Promise.resolve()
      expect(events).toEqual(['cancel'])
      gate.resolve()
      await run
      expect(events).toEqual(['cancel', 'drained', 'ssh', 'primary', 'applied'])
    }
  )

  it('tears down only a non-primary scope without applying the primary connection', async () => {
    const events: string[] = []
    await applyConnectionChange({
      cancelAndWait: async scope => {
        events.push(`cancel:${scope}`)
      },
      isPrimary: false,
      scope: 'worker',
      sendApplied: () => events.push('applied'),
      stopPool: scope => events.push(`pool:${scope}`),
      teardownPrimary: async () => {
        events.push('primary')
      },
      teardownSsh: async scope => {
        events.push(`ssh:${scope}`)
      }
    })
    expect(events).toEqual(['cancel:worker', 'ssh:worker', 'pool:worker'])
  })
})

describe('resolveTerminalConnection', () => {
  it('joins an in-flight backend before resolving the SSH terminal target', async () => {
    const target = { ssh: {}, scope: '' }
    const getTarget = vi.fn().mockReturnValueOnce('pending').mockReturnValueOnce(target)
    const ensureBackend = vi.fn(async () => undefined)

    await expect(resolveTerminalConnection(getTarget, ensureBackend)).resolves.toBe(target)
    expect(ensureBackend).toHaveBeenCalledOnce()
  })

  it('does not start a local terminal while configured SSH remains unavailable', async () => {
    await expect(
      resolveTerminalConnection(
        () => 'pending',
        async () => undefined
      )
    ).rejects.toThrow('not ready')
  })
})

describe('commitConnectionFailure', () => {
  it('prevents a stale bootstrap from publishing failure state', () => {
    const stale = Promise.resolve('stale')
    const current = Promise.resolve('current')
    const commit = vi.fn()

    expect(commitConnectionFailure(current, stale, commit)).toBe(false)
    expect(commit).not.toHaveBeenCalled()
    expect(commitConnectionFailure(current, current, commit)).toBe(true)
    expect(commit).toHaveBeenCalledOnce()
  })
})

describe('applyForegroundConnectionChange', () => {
  it('revokes A before minting and publishing a fresh token for B', async () => {
    const events: string[] = []

    const oldRoute = {
      connectionId: 'remote-a',
      scope: { connection_id: 'remote-a', runtime_instance_id: 'runtime-a', epoch: 1 },
      token: 'foreground-a'
    }

    const nextScope = { connection_id: 'remote-b', runtime_instance_id: 'runtime-b', epoch: 2 }

    const result = await applyForegroundConnectionChange({
      current: oldRoute,
      nextConnectionId: 'remote-b',
      publish: route => {
        events.push(`publish:${route.connectionId}:${route.token}`)
      },
      requestFreshToken: async scope => {
        events.push(`mint:${scope.connection_id}`)

        return 'foreground-b'
      },
      requireScope: async connectionId => {
        events.push(`require:${connectionId}`)

        return nextScope
      },
      revokeForeground: async route => {
        events.push(`revoke:${route.connectionId}:${route.token}`)
      }
    })

    expect(events).toEqual([
      'revoke:remote-a:foreground-a',
      'require:remote-b',
      'mint:remote-b',
      'publish:remote-b:foreground-b'
    ])
    expect(result).toEqual({ connectionId: 'remote-b', scope: nextScope, token: 'foreground-b' })
  })

  it('fails closed after revoking A when B has no valid scope', async () => {
    const publish = vi.fn()
    const requestFreshToken = vi.fn()
    const revokeForeground = vi.fn(async () => {})

    await expect(
      applyForegroundConnectionChange({
        current: {
          connectionId: 'remote-a',
          scope: { connection_id: 'remote-a', runtime_instance_id: 'runtime-a', epoch: 1 },
          token: 'foreground-a'
        },
        nextConnectionId: 'remote-b',
        publish,
        requestFreshToken,
        requireScope: async () => {
          throw new Error('AUTH_REQUIRED')
        },
        revokeForeground
      })
    ).rejects.toThrow('AUTH_REQUIRED')

    expect(revokeForeground).toHaveBeenCalledOnce()
    expect(requestFreshToken).not.toHaveBeenCalled()
    expect(publish).not.toHaveBeenCalled()
  })
})
