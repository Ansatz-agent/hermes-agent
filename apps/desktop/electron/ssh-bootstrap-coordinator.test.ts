import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  buildUnknownHostConfirmation,
  createBootstrapCoordinator,
  openSshWithExplicitHostTrust,
  sshConfigFingerprint
} from './ssh-bootstrap-coordinator'

function deferred() {
  let resolve
  let reject

  const promise = new Promise((ok, fail) => {
    resolve = ok
    reject = fail
  })

  return { promise, reject, resolve }
}

const config = { host: 'box', user: 'alice', port: 22, keyPath: '/key', remoteHermesPath: '/hermes' }

test('sshConfigFingerprint covers scope and every connection field', () => {
  const base = sshConfigFingerprint('', config)
  assert.equal(base, sshConfigFingerprint('', { ...config }))

  for (const [field, value] of Object.entries({
    host: 'other',
    user: 'bob',
    port: 2222,
    keyPath: '/other',
    remoteHermesPath: '/other-hermes',
    remoteProfile: 'default',
    effectiveConfigFingerprint: 'changed-config'
  })) {
    assert.notEqual(base, sshConfigFingerprint('', { ...config, [field]: value }))
  }

  assert.notEqual(base, sshConfigFingerprint('profile', config))
})

test('same scope and fingerprint share one bootstrap', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  let runs = 0

  const first = coordinator.start('', 'same', async () => {
    runs++

    return gate.promise
  })

  const second = coordinator.start('', 'same', async () => {
    runs++

    return 'wrong'
  })

  assert.equal(first, second)
  gate.resolve('done')
  assert.equal(await second, 'done')
  assert.equal(runs, 1)
})

test('changed fingerprint waits for old rollback before starting', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  const events: string[] = []
  let oldLease

  const oldPromise = coordinator.start('', 'old', async lease => {
    oldLease = lease
    events.push('old-start')
    await gate.promise
    events.push('old-rollback')
    lease.assertCurrent()
  })

  await Promise.resolve()

  const newPromise = coordinator.start('', 'new', async lease => {
    events.push('new-start')
    lease.assertCurrent()

    return 'new'
  })

  assert.equal(oldLease.signal.aborted, true)
  await Promise.resolve()
  assert.deepEqual(events, ['old-start'])
  gate.resolve()
  await assert.rejects(oldPromise, (error: any) => error.kind === 'superseded')
  assert.equal(await newPromise, 'new')
  assert.deepEqual(events, ['old-start', 'old-rollback', 'new-start'])
})

test('forceCleanupAll runs registered pending resource cleanup', async () => {
  const coordinator = createBootstrapCoordinator()
  const gate = deferred()
  let cleaned = 0

  const promise = coordinator.start('', 'x', async lease => {
    lease.onForceCleanup(async () => {
      cleaned++
    })
    await gate.promise
  })

  await Promise.resolve()
  await coordinator.forceCleanupAll()
  assert.equal(cleaned, 1)
  gate.resolve()
  await promise
})

test('cancelAll invalidates every pending scope and exposes promises for quit', async () => {
  const coordinator = createBootstrapCoordinator()
  const gates = [deferred(), deferred()]

  const promises = gates.map((gate, index) =>
    coordinator.start(String(index), 'x', async lease => {
      await gate.promise
      lease.assertCurrent()
    })
  )

  assert.equal(coordinator.promises().length, 2)
  coordinator.cancelAll()
  gates.forEach(gate => gate.resolve())
  const results = await Promise.allSettled(promises)
  assert.ok(results.every(result => result.status === 'rejected' && (result.reason as any).kind === 'superseded'))
})

test('cancelAndWait drains only the requested scope', async () => {
  const coordinator = createBootstrapCoordinator()
  const firstGate = deferred()
  const secondGate = deferred()

  const first = coordinator.start('first', 'x', async lease => {
    await firstGate.promise
    lease.assertCurrent()
  })

  const second = coordinator.start('second', 'x', async lease => {
    await secondGate.promise
    lease.assertCurrent()

    return 'second'
  })

  await Promise.resolve()
  let drained = false

  const drain = coordinator.cancelAndWait('first').then(() => {
    drained = true
  })

  await Promise.resolve()
  assert.equal(drained, false)
  firstGate.resolve()
  await drain
  await assert.rejects(first, (error: any) => error.kind === 'superseded')
  assert.equal(coordinator.pending.has('second'), true)
  secondGate.resolve()
  assert.equal(await second, 'second')
})

test('a generation started during cancelAndWait cannot run before the drain completes', async () => {
  const coordinator = createBootstrapCoordinator()
  const oldGate = deferred()
  const events: string[] = []

  const old = coordinator.start('scope', 'old', async lease => {
    events.push('old-start')
    await oldGate.promise
    lease.assertCurrent()
  })

  await Promise.resolve()
  const drain = coordinator.cancelAndWait('scope')

  const next = coordinator.start('scope', 'new', async () => {
    events.push('new-start')

    return 'new'
  })

  await Promise.resolve()
  assert.deepEqual(events, ['old-start'])
  oldGate.resolve()
  await drain
  await assert.rejects(old, (error: any) => error.kind === 'superseded')
  assert.equal(await next, 'new')
  assert.deepEqual(events, ['old-start', 'new-start'])
})

const unknownHost = {
  algorithm: 'ssh-ed25519',
  fingerprint: 'SHA256:displayed',
  host: 'box',
  knownHostsEntry: 'box ssh-ed25519 ZGlzcGxheWVk',
  port: 22
}

test('unknown host confirmation warns that keyscan is not identity proof', () => {
  const copy = buildUnknownHostConfirmation(unknownHost)

  assert.match(copy.detail, /ssh-keyscan is not proof of identity/i)
  assert.match(copy.detail, /trusted administrator|out-of-band/i)
  assert.match(copy.detail, /SHA256:displayed/)
})

test('unknown host requires confirmation, re-scan, atomic append, then strict retry', async () => {
  const events: string[] = []
  let opens = 0
  const unknownError: any = new Error('unknown')
  unknownError.kind = 'unknown-host-key'

  await openSshWithExplicitHostTrust({
    append: async candidate => {
      events.push(`append:${candidate.fingerprint}`)
    },
    confirm: async candidate => {
      events.push(`confirm:${candidate.fingerprint}`)

      return true
    },
    openStrict: async () => {
      opens += 1
      events.push('open-strict')

      if (opens === 1) {
        throw unknownError
      }
    },
    scan: async () => {
      events.push('scan')

      return unknownHost
    }
  })

  assert.deepEqual(events, [
    'open-strict',
    'scan',
    'confirm:SHA256:displayed',
    'scan',
    'append:SHA256:displayed',
    'open-strict'
  ])
})

test('cancel leaves known_hosts unchanged and never retries or starts auth bridge', async () => {
  const events: string[] = []
  const unknownError: any = new Error('unknown')
  unknownError.kind = 'unknown-host-key'

  await assert.rejects(
    async () => {
      await openSshWithExplicitHostTrust({
        append: async () => events.push('append'),
        confirm: async () => false,
        openStrict: async () => {
          events.push('open-strict')
          throw unknownError
        },
        scan: async () => unknownHost
      })
      events.push('auth-bridge')
    },
    (error: any) => error.kind === 'host-key-untrusted'
  )

  assert.deepEqual(events, ['open-strict'])
})

test('changed host key fails closed before scan, append, or auth bridge', async () => {
  const events: string[] = []
  const changedError: any = new Error('changed')
  changedError.kind = 'host-key-changed'

  await assert.rejects(async () => {
    await openSshWithExplicitHostTrust({
      append: async () => events.push('append'),
      confirm: async () => {
        events.push('confirm')

        return true
      },
      openStrict: async () => {
        events.push('open-strict')
        throw changedError
      },
      scan: async () => {
        events.push('scan')

        return unknownHost
      }
    })
    events.push('auth-bridge')
  }, changedError)

  assert.deepEqual(events, ['open-strict'])
})

test('key change between confirmation and append fails closed', async () => {
  const first = unknownHost

  const changed = {
    ...unknownHost,
    fingerprint: 'SHA256:changed',
    knownHostsEntry: 'box ssh-ed25519 Y2hhbmdlZA=='
  }

  const candidates = [first, changed]
  const unknownError: any = new Error('unknown')
  unknownError.kind = 'unknown-host-key'
  let appended = false

  await assert.rejects(
    openSshWithExplicitHostTrust({
      append: async () => {
        appended = true
      },
      confirm: async () => true,
      openStrict: async () => {
        throw unknownError
      },
      scan: async () => candidates.shift()!
    }),
    (error: any) => error.kind === 'host-key-changed'
  )
  assert.equal(appended, false)
})
