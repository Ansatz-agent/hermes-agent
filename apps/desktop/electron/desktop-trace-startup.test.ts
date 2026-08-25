import assert from 'node:assert/strict'

import { afterEach, test, vi } from 'vitest'

import { LocalTraceCaptureController, prepareLocalTraceCapture } from './desktop-trace-startup'

afterEach(() => vi.useRealTimers())

type Scope = { id: string }
type Capture = { id: string }

function deferred<T>() {
  let resolve: (value: T) => void
  let reject: (error: Error) => void

  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })

  return { promise, reject: reject!, resolve: resolve! }
}

function controllerHarness(starts: Array<ReturnType<typeof deferred<Capture>>>) {
  let currentScope: Scope | null = { id: 'A' }
  const diagnostics: string[] = []
  let startCalls = 0
  const stopped: string[] = []

  const controller = new LocalTraceCaptureController<Scope, Capture>({
    isScopeCurrent: scope => scope.id === currentScope?.id,
    onDiagnostic: message => diagnostics.push(message),
    retryMs: 20,
    sameScope: (left, right) => left.id === right.id,
    setupTimeoutMs: 10,
    startCapture: async () => {
      startCalls += 1
      const next = starts.shift()

      if (!next) {
        throw new Error('unexpected listener start')
      }

      return next.promise
    },
    stopCapture: async capture => {
      stopped.push(capture.id)
    }
  })

  return {
    controller,
    diagnostics,
    startCalls: () => startCalls,
    setCurrentScope: (scope: Scope | null) => {
      currentScope = scope
    },
    stopped
  }
}

test('backend preparation continues when local trace capture cannot start', async () => {
  const events: string[] = []

  const result = await prepareLocalTraceCapture({
    startListener: async () => Promise.reject(new Error('local listener unavailable')),
    onDiagnostic: message => events.push(`diagnostic:${message}`),
    scheduleRetry: () => events.push('retry')
  })

  events.push('spawn-backend')

  assert.equal(result, null)
  assert.deepEqual(events, ['diagnostic:trace capture unavailable', 'retry', 'spawn-backend'])
})

test('trace capture reports no listener error details', async () => {
  const diagnostics: string[] = []

  await prepareLocalTraceCapture({
    startListener: async () => Promise.reject(new Error('native session token must remain secret')),
    onDiagnostic: message => diagnostics.push(message),
    scheduleRetry: () => {}
  })

  assert.deepEqual(diagnostics, ['trace capture unavailable'])
})

test('a timed-out listener retry starts fresh and keeps backend preparation available', async () => {
  vi.useFakeTimers()
  const first = deferred<Capture>()
  const second = deferred<Capture>()
  const { controller, diagnostics } = controllerHarness([first, second])

  const initial = controller.prepare({ id: 'A' })

  await vi.advanceTimersByTimeAsync(10)
  assert.equal(await initial, null)
  assert.deepEqual(diagnostics, ['trace capture unavailable'])

  await vi.advanceTimersByTimeAsync(20)
  second.resolve({ id: 'second' })
  await vi.runAllTimersAsync()

  assert.deepEqual(controller.context({ id: 'A' }), { id: 'second' })
})

test('a late stale listener stops without replacing the fresh capture', async () => {
  vi.useFakeTimers()
  const first = deferred<Capture>()
  const second = deferred<Capture>()
  const { controller, stopped } = controllerHarness([first, second])

  const initial = controller.prepare({ id: 'A' })
  await vi.advanceTimersByTimeAsync(10)
  await initial
  await vi.advanceTimersByTimeAsync(20)
  second.resolve({ id: 'second' })
  await vi.runAllTimersAsync()

  first.resolve({ id: 'first' })
  await vi.advanceTimersByTimeAsync(0)

  assert.deepEqual(stopped, ['first'])
  assert.deepEqual(controller.context({ id: 'A' }), { id: 'second' })
})

test('a new scope bypasses a hung old-scope listener', async () => {
  vi.useFakeTimers()
  const first = deferred<Capture>()
  const second = deferred<Capture>()
  const { controller, setCurrentScope, stopped } = controllerHarness([first, second])

  const pendingA = controller.prepare({ id: 'A' })
  setCurrentScope({ id: 'B' })
  const pendingB = controller.prepare({ id: 'B' })

  second.resolve({ id: 'second' })
  assert.deepEqual(await pendingB, { id: 'second' })
  assert.equal(await pendingA, null)

  first.resolve({ id: 'first' })
  await vi.advanceTimersByTimeAsync(0)

  assert.deepEqual(stopped, ['first'])
  assert.deepEqual(controller.context({ id: 'B' }), { id: 'second' })
})

test('terminal scope cancellation prevents a scheduled retry', async () => {
  vi.useFakeTimers()
  const first = deferred<Capture>()
  const { controller, setCurrentScope, startCalls } = controllerHarness([first])

  const initial = controller.prepare({ id: 'A' })
  await vi.advanceTimersByTimeAsync(10)
  assert.equal(await initial, null)
  setCurrentScope(null)
  await controller.stop()
  await vi.advanceTimersByTimeAsync(20)

  assert.equal(startCalls(), 1)
  assert.equal(controller.context({ id: 'A' }), null)
})
