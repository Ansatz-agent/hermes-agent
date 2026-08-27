import assert from 'node:assert/strict'
import { type ChildProcessWithoutNullStreams, spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { test } from 'vitest'

import { issueAuthScopeToken } from './auth-scope-token'
import { BackendControlChannel } from './backend-control-channel'
import { BackendHttpError, requestJsonWithLocalCapability } from './backend-json-client'
import {
  type LocalCapabilityBinding,
  type LocalCapabilityDiagnostic,
  LocalCapabilityManager
} from './local-capability-manager'

const REPO_ROOT = fileURLToPath(new URL('../../..', import.meta.url))
const FIXTURE_PATH = path.join(REPO_ROOT, 'tests', 'fixtures', 'desktop_scope_control_backend.py')

const DEFAULT_PYTHON = path.join(
  REPO_ROOT,
  '.venv',
  process.platform === 'win32' ? path.join('Scripts', 'python.exe') : path.join('bin', 'python')
)

const PYTHON = process.env.HERMES_PYTHON || DEFAULT_PYTHON

const SCOPE = {
  connection_id: 'local',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  epoch: 7
}

type AckOperation = 'scope_token_registered' | 'scope_token_promoted'
type AckSelector = `${AckOperation}:${number}`

type FixtureOptions = {
  ackDelayMs?: number
  ackDelaySelectors?: readonly AckSelector[]
  dropAckSelectors?: readonly AckSelector[]
  duplicateAcks?: boolean
  outOfOrderAcks?: boolean
}

type FixtureCounts = {
  config_writes: number
  model_requests: number
  trace_uploads: number
  ws_messages: string[]
}

class PythonScopeFixture {
  readonly binding: LocalCapabilityBinding
  readonly diagnostics: LocalCapabilityDiagnostic[]
  readonly logs: string[]
  readonly stderr: string[] = []
  readonly child: ChildProcessWithoutNullStreams
  readonly control: BackendControlChannel
  readonly manager: LocalCapabilityManager
  readonly bearers: string[]
  private readonly exit: Promise<void>
  private socket: WebSocket | null = null

  private constructor(
    child: ChildProcessWithoutNullStreams,
    control: BackendControlChannel,
    manager: LocalCapabilityManager,
    baseUrl: string,
    diagnostics: LocalCapabilityDiagnostic[],
    logs: string[],
    bearers: string[],
    scope: typeof SCOPE
  ) {
    this.child = child
    this.control = control
    this.manager = manager
    this.diagnostics = diagnostics
    this.logs = logs
    this.bearers = bearers
    this.binding = {
      key: 'primary',
      baseUrl,
      scope,
      backendGeneration: 1,
      control
    }
    this.exit = new Promise(resolve => child.once('exit', () => resolve()))
    child.stderr.on('data', chunk => this.stderr.push(String(chunk)))
  }

  static async start(options: FixtureOptions = {}): Promise<PythonScopeFixture> {
    assert.equal(fs.existsSync(FIXTURE_PATH), true, `Missing Python fixture: ${FIXTURE_PATH}`)
    assert.equal(fs.existsSync(PYTHON), true, `Missing Python runtime: ${PYTHON}`)

    const args = ['-u', FIXTURE_PATH]

    if (options.ackDelayMs) {
      args.push('--ack-delay-ms', String(options.ackDelayMs))
    }

    if (options.ackDelaySelectors?.length) {
      args.push('--ack-delay-selectors', options.ackDelaySelectors.join(','))
    }

    if (options.dropAckSelectors?.length) {
      args.push('--drop-ack-selectors', options.dropAckSelectors.join(','))
    }

    if (options.duplicateAcks) {
      args.push('--duplicate-acks')
    }

    if (options.outOfOrderAcks) {
      args.push('--out-of-order-acks')
    }

    const child = spawn(PYTHON, args, {
      cwd: REPO_ROOT,
      env: { ...process.env, PYTHONPATH: REPO_ROOT },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true
    })

    const logs: string[] = []
    const diagnostics: LocalCapabilityDiagnostic[] = []
    const bearers: string[] = []

    const manager = new LocalCapabilityManager({
      issueToken: scope => {
        const token = issueAuthScopeToken(scope)
        bearers.push(token.bearer)

        return token
      },
      onDiagnostic: event => diagnostics.push(event),
      random: () => 0
    })

    let control!: BackendControlChannel
    control = new BackendControlChannel(child, {
      onClose: () => manager.revokeByControl(control),
      onLog: line => logs.push(line)
    })

    try {
      const ready = await control.waitForReady({ timeoutMs: 15_000 })
      assert.equal(ready.desktopScopeProtocol, 2)

      return new PythonScopeFixture(
        child,
        control,
        manager,
        `http://127.0.0.1:${ready.port}`,
        diagnostics,
        logs,
        bearers,
        SCOPE
      )
    } catch (error) {
      child.kill()
      throw error
    }
  }

  async activate(): Promise<void> {
    await this.manager.activate(this.binding)
  }

  async refresh(): Promise<void> {
    await this.manager.refresh(this.binding.key, 'recovery')
  }

  request<T>(pathName: string, method = 'GET', body?: unknown): Promise<T> {
    return requestJsonWithLocalCapability<T>({
      manager: this.manager,
      key: this.binding.key,
      url: new URL(pathName, this.binding.baseUrl).toString(),
      method,
      body,
      timeoutMs: 20_000
    })
  }

  putConfig(index: number, delayMs = 50): Promise<unknown> {
    return this.request('/api/config', 'PUT', { model: `model-${index}`, delay_ms: delayMs })
  }

  requestModel(index: number, provider401 = false, delayMs = 50): Promise<unknown> {
    return this.request('/api/model', 'POST', {
      prompt: `prompt-${index}`,
      provider_401: provider401,
      delay_ms: delayMs
    })
  }

  uploadTrace(index: number, delayMs = 50): Promise<unknown> {
    return this.request('/v1/traces', 'POST', { span: index, delay_ms: delayMs })
  }

  async counts(): Promise<FixtureCounts> {
    return this.request<FixtureCounts>('/api/status')
  }

  async connectWs(): Promise<void> {
    const bearer = this.manager.snapshot(this.binding.key).bearer
    const url = new URL('/ws', this.binding.baseUrl)
    url.protocol = 'ws:'
    const socket = new WebSocket(url, ['ansatz.scope.v2', bearer])

    await new Promise<void>((resolve, reject) => {
      socket.addEventListener('open', () => resolve(), { once: true })
      socket.addEventListener('error', () => reject(new Error('fixture websocket failed to open')), { once: true })
    })
    this.socket = socket
  }

  async sendWsMessage(message: string): Promise<void> {
    assert.ok(this.socket && this.socket.readyState === WebSocket.OPEN)
    const socket = this.socket

    const echoed = new Promise<string>((resolve, reject) => {
      socket.addEventListener('message', event => resolve(String(event.data)), { once: true })
      socket.addEventListener('error', () => reject(new Error('fixture websocket failed')), { once: true })
    })

    socket.send(message)
    assert.equal(await echoed, message)
  }

  allOutput(): string {
    return [...this.logs, ...this.stderr].join('\n')
  }

  issuedBearers(): string[] {
    return [...this.bearers]
  }

  async rawStatus(bearer: string): Promise<{ body: string; status: number }> {
    const response = await fetch(new URL('/api/status', this.binding.baseUrl), {
      headers: { 'X-Hermes-Session-Token': bearer }
    })

    return { body: await response.text(), status: response.status }
  }

  async waitForRawStatus(
    bearer: string,
    expectedStatus: number,
    timeoutMs = 3_000
  ): Promise<{ body: string; status: number }> {
    const deadline = Date.now() + timeoutMs
    let latest: { body: string; status: number } | null = null

    while (Date.now() < deadline) {
      latest = await this.rawStatus(bearer)

      if (latest.status === expectedStatus) {
        return latest
      }

      await new Promise(resolve => setTimeout(resolve, 25))
    }

    throw new Error(`fixture status did not become ${expectedStatus}; latest=${latest?.status ?? 'unavailable'}`)
  }

  async terminateBackend(): Promise<void> {
    this.child.kill()
    await this.exit
  }

  async close(): Promise<void> {
    this.socket?.close()
    this.socket = null
    this.manager.revoke(this.binding.key)

    if (this.child.exitCode === null && this.child.signalCode === null) {
      this.child.kill()
    }

    await Promise.race([this.exit, new Promise(resolve => setTimeout(resolve, 5_000))])
  }
}

test('survives three real Node-Python rotations while writes and sockets stay live', async () => {
  const fixture = await PythonScopeFixture.start({ ackDelayMs: 500 })

  try {
    await fixture.activate()
    await fixture.connectWs()
    const writes: Promise<unknown>[] = []
    const models: Promise<unknown>[] = []
    const traces: Promise<unknown>[] = []
    const batchSizes = [14, 13, 13]
    let requestIndex = 0

    for (const [rotationIndex, batchSize] of batchSizes.entries()) {
      let completedBatchRequests = 0

      const trackBatch = (operation: Promise<unknown>): Promise<unknown> =>
        operation.finally(() => {
          completedBatchRequests += 1
        })

      for (let offset = 0; offset < batchSize; offset += 1) {
        const index = requestIndex + offset
        writes.push(trackBatch(fixture.putConfig(index, 1_500)))
        models.push(trackBatch(fixture.requestModel(index, false, 1_500)))
        traces.push(trackBatch(fixture.uploadTrace(index, 1_500)))
      }

      requestIndex += batchSize
      const batchRequestCount = batchSize * 3

      await fixture.refresh()
      assert.ok(
        completedBatchRequests < batchRequestCount,
        `rotation ${rotationIndex} completed after all ${batchRequestCount} current-batch requests had already settled`
      )
      await fixture.sendWsMessage(`rotation-${rotationIndex}`)
    }

    await Promise.all([...writes, ...models, ...traces])
    assert.deepEqual(await fixture.counts(), {
      config_writes: 40,
      model_requests: 40,
      trace_uploads: 40,
      ws_messages: ['rotation-0', 'rotation-1', 'rotation-2']
    })
    assert.ok(fixture.logs.includes('desktop-scope-fixture-log-sentinel'))
    assert.equal(
      fixture.logs.some(line => line.startsWith('ANSATZ_SCOPE_CONTROL_V2')),
      false
    )
    assert.equal(fixture.allOutput().includes('Ansatz login required'), false)

    for (const bearer of fixture.issuedBearers()) {
      assert.equal(fixture.allOutput().includes(bearer), false)
    }
  } finally {
    await fixture.close()
  }
}, 30_000)

test.each([
  ['lost registration ACK', { dropAckSelectors: ['scope_token_registered:2'] }],
  ['duplicate and out-of-order ACKs', { duplicateAcks: true, outOfOrderAcks: true }],
  ['five-second registration ACK delay', { ackDelayMs: 5_100, ackDelaySelectors: ['scope_token_registered:2'] }],
  ['lost promotion ACK after atomic promotion', { dropAckSelectors: ['scope_token_promoted:2'] }]
] as const)(
  'recovers a real rotation after %s without exposing login UI text',
  async (_label, options) => {
    const fixture = await PythonScopeFixture.start(options)

    try {
      await fixture.activate()
      const before = fixture.manager.snapshot(fixture.binding.key)
      await fixture.refresh()
      const after = fixture.manager.snapshot(fixture.binding.key)

      assert.notEqual(after.registrationId, before.registrationId)
      assert.equal(fixture.allOutput().includes('Ansatz login required'), false)
    } finally {
      await fixture.close()
    }
  },
  20_000
)

test('preserves a real provider 401 and does not rotate the local capability', async () => {
  const fixture = await PythonScopeFixture.start()

  try {
    await fixture.activate()
    const before = fixture.manager.snapshot(fixture.binding.key)

    await assert.rejects(
      fixture.requestModel(1, true),
      error =>
        error instanceof BackendHttpError &&
        error.status === 401 &&
        error.code === 'provider_unauthorized' &&
        error.message.includes('provider rejected fixture request')
    )

    assert.equal(fixture.manager.snapshot(fixture.binding.key).registrationId, before.registrationId)
    assert.equal(fixture.allOutput().includes('Ansatz login required'), false)
  } finally {
    await fixture.close()
  }
})

test('logout during overlap revokes pending rotation without emitting login text', async () => {
  const fixture = await PythonScopeFixture.start({ ackDelayMs: 500 })

  try {
    await fixture.activate()
    const bearer = fixture.manager.snapshot(fixture.binding.key).bearer
    const refresh = fixture.manager.refresh(fixture.binding.key, 'recovery')
    await new Promise(resolve => setTimeout(resolve, 50))
    fixture.manager.revoke(fixture.binding.key)
    fixture.child.stdin.end()

    await assert.rejects(refresh, /Local backend capability unavailable/)
    const rejected = await fixture.waitForRawStatus(bearer, 401)
    assert.equal(rejected.status, 401)
    assert.equal(rejected.body.includes('Ansatz login required'), false)
    assert.equal(fixture.allOutput().includes('Ansatz login required'), false)
  } finally {
    await fixture.close()
  }
})

test('control EOF revokes the previous bearer in the same backend process', async () => {
  const fixture = await PythonScopeFixture.start()

  try {
    await fixture.activate()
    const oldBearer = fixture.manager.snapshot(fixture.binding.key).bearer
    assert.equal((await fixture.rawStatus(oldBearer)).status, 200)

    fixture.child.stdin.end()
    const rejected = await fixture.waitForRawStatus(oldBearer, 401)

    assert.equal(rejected.status, 401)
    assert.equal(rejected.body.includes('Ansatz login required'), false)
    assert.equal(fixture.child.exitCode, null)
    assert.equal(fixture.child.signalCode, null)
  } finally {
    await fixture.close()
  }
})

test('fails closed when the real backend process exits', async () => {
  const fixture = await PythonScopeFixture.start()

  try {
    await fixture.activate()
    await fixture.terminateBackend()
    await assert.rejects(fixture.refresh(), /Local backend capability unavailable/)
    assert.equal(fixture.allOutput().includes('Ansatz login required'), false)
  } finally {
    await fixture.close()
  }
})
