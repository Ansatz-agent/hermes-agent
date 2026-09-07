import type fs from 'node:fs'
import { StringDecoder } from 'node:string_decoder'

import {
  AUTH_SCOPE_TOKEN_OVERLAP_SECONDS,
  AUTH_SCOPE_TOKEN_TTL_SECONDS,
  type ScopeControlAck
} from './auth-scope-token'
import {
  type BackendReady,
  parseBackendReadyLine,
  readBackendReadyFile,
  resolvePortAnnounceTimeoutMs
} from './backend-ready'

const CONTROL_ACK_MARKER = 'ANSATZ_SCOPE_CONTROL_V2'
const CONTROL_ACK_PREFIX = `${CONTROL_ACK_MARKER} `
const MAX_CONTROL_ACK_LINE_BYTES = 4_096
const MAX_STDOUT_LINE_BYTES = 1_048_576
const DEFAULT_ACK_TIMEOUT_MS = 5_000

export type ChildProcessLike = {
  stdin: NodeJS.WritableStream & { destroyed?: boolean; writable?: boolean }
  stdout: NodeJS.ReadableStream
  on(event: 'error' | 'exit', listener: (...args: any[]) => void): unknown
  off(event: 'error' | 'exit', listener: (...args: any[]) => void): unknown
}

type ReadyWaiter = {
  resolve: (ready: BackendReady) => void
  reject: (error: Error) => void
  timer: NodeJS.Timeout
  interval: NodeJS.Timeout | null
}

type AckWaiter = {
  match: (value: ScopeControlAck) => boolean
  resolve: (value: ScopeControlAck) => void
  reject: (error: Error) => void
  timer: NodeJS.Timeout
}

function asError(value: unknown, fallback: string): Error {
  return value instanceof Error ? value : new Error(fallback)
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort()
  const wanted = [...expected].sort()

  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index])
}

function validControlId(value: unknown): value is string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) {
    return false
  }

  try {
    const decoded = Buffer.from(value, 'base64url')

    return decoded.byteLength === 16 && decoded.toString('base64url') === value
  } catch {
    return false
  }
}

function validConnectionId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= 128 &&
    ![...value].some(character => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) === 0x7f)
  )
}

function validRuntimeInstanceId(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{32}$/.test(value)
}

function validEpoch(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0
}

function validSeconds(value: unknown, maximum: number): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= maximum
}

function parseScopeControlAck(line: string): ScopeControlAck | null {
  let value: unknown

  try {
    value = JSON.parse(line)
  } catch {
    return null
  }

  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>

  if (record.version !== 2) {
    return null
  }

  if (record.operation === 'scope_token_registered') {
    if (
      !exactKeys(record, [
        'version',
        'operation',
        'registration_id',
        'connection_id',
        'runtime_instance_id',
        'epoch',
        'ttl_seconds'
      ]) ||
      !validControlId(record.registration_id) ||
      !validConnectionId(record.connection_id) ||
      !validRuntimeInstanceId(record.runtime_instance_id) ||
      !validEpoch(record.epoch) ||
      !validSeconds(record.ttl_seconds, AUTH_SCOPE_TOKEN_TTL_SECONDS)
    ) {
      return null
    }

    return record as ScopeControlAck
  }

  if (record.operation === 'scope_token_promoted') {
    if (
      !exactKeys(record, [
        'version',
        'operation',
        'transition_id',
        'registration_id',
        'previous_registration_id',
        'connection_id',
        'runtime_instance_id',
        'epoch',
        'overlap_seconds'
      ]) ||
      !validControlId(record.transition_id) ||
      !validControlId(record.registration_id) ||
      !(record.previous_registration_id === null || validControlId(record.previous_registration_id)) ||
      !validConnectionId(record.connection_id) ||
      !validRuntimeInstanceId(record.runtime_instance_id) ||
      !validEpoch(record.epoch) ||
      !validSeconds(record.overlap_seconds, AUTH_SCOPE_TOKEN_OVERLAP_SECONDS)
    ) {
      return null
    }

    return record as ScopeControlAck
  }

  return null
}

export class BackendControlChannel {
  private readonly child: ChildProcessLike
  private readonly onClose: (reason: Error) => void
  private readonly onLog: (line: string) => void
  private readonly readyWaiters = new Set<ReadyWaiter>()
  private readonly ackWaiters: AckWaiter[] = []
  private readonly stdoutDecoder = new StringDecoder('utf8')
  private stdoutBuffer = ''
  private ready: BackendReady | null = null
  private closedReason: Error | null = null

  constructor(child: ChildProcessLike, options: { onClose?: (reason: Error) => void; onLog: (line: string) => void }) {
    this.child = child
    this.onClose = options.onClose ?? (() => undefined)
    this.onLog = options.onLog
    child.stdout.on('data', this.handleStdout)
    child.on('error', this.handleChildError)
    child.on('exit', this.handleChildExit)
  }

  waitForReady(options: { readyFile?: fs.PathOrFileDescriptor; timeoutMs?: number } = {}): Promise<BackendReady> {
    if (this.closedReason) {
      return Promise.reject(this.closedReason)
    }

    if (this.ready) {
      return Promise.resolve(this.ready)
    }

    const timeoutMs = options.timeoutMs ?? resolvePortAnnounceTimeoutMs()

    return new Promise((resolve, reject) => {
      const waiter: ReadyWaiter = {
        resolve,
        reject,
        timer: setTimeout(() => {
          this.removeReadyWaiter(waiter)
          reject(new Error(`Timed out waiting for Hermes backend port announcement (${timeoutMs}ms)`))
        }, timeoutMs),
        interval: null
      }

      this.readyWaiters.add(waiter)

      if (options.readyFile) {
        const checkReadyFile = () => {
          const ready = readBackendReadyFile(options.readyFile!)

          if (ready) {
            this.acceptReady(ready)
          }
        }

        waiter.interval = setInterval(checkReadyFile, 50)
        waiter.interval.unref?.()
        checkReadyFile()
      }
    })
  }

  expectAck(match: (value: ScopeControlAck) => boolean, timeoutMs: number): Promise<ScopeControlAck> {
    if (this.closedReason) {
      return Promise.reject(this.closedReason)
    }

    return new Promise((resolve, reject) => {
      const waiter: AckWaiter = {
        match,
        resolve,
        reject,
        timer: setTimeout(() => {
          this.removeAckWaiter(waiter)
          reject(new Error(`Hermes scope control ACK timeout (${timeoutMs}ms)`))
        }, timeoutMs)
      }

      this.ackWaiters.push(waiter)
    })
  }

  request(
    frame: string,
    match: (value: ScopeControlAck) => boolean,
    timeoutMs = DEFAULT_ACK_TIMEOUT_MS
  ): Promise<ScopeControlAck> {
    const ack = this.expectAck(match, timeoutMs)

    if (this.closedReason) {
      return ack
    }

    if (this.child.stdin.destroyed || this.child.stdin.writable === false) {
      this.close(new Error('Backend control stdin is not writable'))

      return ack
    }

    try {
      this.child.stdin.write(frame, error => {
        if (error) {
          this.close(asError(error, 'Backend control stdin write failed'))
        }
      })
    } catch (error) {
      this.close(asError(error, 'Backend control stdin write failed'))
    }

    return ack
  }

  close(reason = new Error('Backend control channel closed')): void {
    if (this.closedReason) {
      return
    }

    this.closedReason = reason
    this.child.stdout.off('data', this.handleStdout)
    this.child.off('error', this.handleChildError)
    this.child.off('exit', this.handleChildExit)
    this.stdoutBuffer = ''

    for (const waiter of [...this.readyWaiters]) {
      this.removeReadyWaiter(waiter)
      waiter.reject(reason)
    }

    for (const waiter of [...this.ackWaiters]) {
      this.removeAckWaiter(waiter)
      waiter.reject(reason)
    }

    try {
      this.onClose(reason)
    } catch {
      // Closing the channel is authoritative even if optional cleanup fails.
    }
  }

  private readonly handleStdout = (chunk: unknown): void => {
    if (this.closedReason) {
      return
    }

    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), 'utf8')

    this.stdoutBuffer += this.stdoutDecoder.write(bytes)
    let newline = this.stdoutBuffer.indexOf('\n')

    while (newline !== -1) {
      const line = this.stdoutBuffer.slice(0, newline)
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1)

      if (Buffer.byteLength(line, 'utf8') > MAX_STDOUT_LINE_BYTES) {
        this.close(new Error('Backend control stdout line exceeded the size limit'))

        return
      }

      this.routeLine(line)

      if (this.closedReason) {
        return
      }

      newline = this.stdoutBuffer.indexOf('\n')
    }

    if (Buffer.byteLength(this.stdoutBuffer, 'utf8') > MAX_STDOUT_LINE_BYTES) {
      this.close(new Error('Backend control stdout line exceeded the size limit'))
    }
  }

  private readonly handleChildError = (error: unknown): void => {
    this.close(asError(error, 'Backend control child error'))
  }

  private readonly handleChildExit = (code: unknown, signal: unknown): void => {
    this.close(new Error(`Backend control channel closed: child exited (${String(signal || code)})`))
  }

  private routeLine(line: string): void {
    if (line.startsWith(CONTROL_ACK_MARKER)) {
      if (!line.startsWith(CONTROL_ACK_PREFIX) || Buffer.byteLength(line, 'utf8') > MAX_CONTROL_ACK_LINE_BYTES) {
        return
      }

      const ack = parseScopeControlAck(line.slice(CONTROL_ACK_PREFIX.length))

      if (ack) {
        this.routeAck(ack)
      }

      return
    }

    const ready = parseBackendReadyLine(line)

    if (ready) {
      this.acceptReady(ready)

      return
    }

    this.onLog(line.endsWith('\r') ? line.slice(0, -1) : line)
  }

  private routeAck(ack: ScopeControlAck): void {
    for (const waiter of [...this.ackWaiters]) {
      let matched = false

      try {
        matched = waiter.match(ack)
      } catch (error) {
        this.removeAckWaiter(waiter)
        waiter.reject(asError(error, 'Scope control ACK matcher failed'))

        continue
      }

      if (matched) {
        this.removeAckWaiter(waiter)
        waiter.resolve(ack)

        return
      }
    }
  }

  private acceptReady(ready: BackendReady): void {
    if (this.ready || this.closedReason) {
      return
    }

    this.ready = ready

    for (const waiter of [...this.readyWaiters]) {
      this.removeReadyWaiter(waiter)
      waiter.resolve(ready)
    }
  }

  private removeReadyWaiter(waiter: ReadyWaiter): void {
    if (!this.readyWaiters.delete(waiter)) {
      return
    }

    clearTimeout(waiter.timer)

    if (waiter.interval) {
      clearInterval(waiter.interval)
    }
  }

  private removeAckWaiter(waiter: AckWaiter): void {
    const index = this.ackWaiters.indexOf(waiter)

    if (index === -1) {
      return
    }

    this.ackWaiters.splice(index, 1)
    clearTimeout(waiter.timer)
  }
}
