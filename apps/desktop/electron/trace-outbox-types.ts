const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const ACCOUNT_KEY = /^(?:account-[0-9a-f-]{36}|legacy-[0-9a-f]{64})$/

export interface TraceOwner {
  accountKey: string
  accountId: string | null
  sessionId: string | null
  installationId: string
}

export interface TraceOwnerState {
  owner: TraceOwner
  uploadable: boolean
}

export interface TraceEnvelopeInput {
  body: Buffer
  contentType: 'application/x-protobuf'
  entrypoint: 'cli' | 'dashboard' | 'desktop' | 'voice'
  hermesSessionId: string
  owner: TraceOwner
  runId: string
  telemetrySchemaVersion: string
}

export interface DurableTraceBatch extends TraceEnvelopeInput {
  attempt: number
  batchId: string
  createdAt: number
  lastErrorClass: string | null
  nextRetryAt: number
  payloadSha256: string
  sequence: number
}

export interface DurableReceipt {
  batchId: string
  outcome: 'accepted' | 'duplicate'
  receivedAt: number
}

export interface TraceOutboxDiagnostics {
  accepted: number
  deduplicated: number
  duplicate: number
  evictedCapacity: number
  expired: number
  pending: number
  pendingBytes: number
  quarantined: number
  recoveredCorruptTail: number
}

export type OutboxSendResult = { kind: 'accepted' | 'duplicate' } | { kind: 'quarantined' } | { kind: 'retry' }

export function validateTraceOwner(owner: TraceOwner): TraceOwnerState {
  if (!ACCOUNT_KEY.test(owner.accountKey) || !UUID_V4.test(owner.installationId)) {
    throw new TypeError('invalid_account_key')
  }

  const uploadable = owner.accountId !== null && owner.sessionId !== null

  if (uploadable && (!UUID_V4.test(owner.accountId!) || !UUID_V4.test(owner.sessionId!))) {
    throw new TypeError('invalid_trace_owner')
  }

  return { owner: { ...owner }, uploadable }
}
