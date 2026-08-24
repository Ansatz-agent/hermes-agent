const CANONICAL_UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const ACCOUNT_KEY = /^(?:account-[0-9a-f-]{36}|legacy-[0-9a-f]{64})$/

export function isCanonicalUuidV4(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_UUID_V4.test(value)
}

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
  if (!ACCOUNT_KEY.test(owner.accountKey) || !isCanonicalUuidV4(owner.installationId)) {
    throw new TypeError('invalid_account_key')
  }

  if (owner.accountKey.startsWith('legacy-')) {
    if (owner.accountId !== null || owner.sessionId !== null) {
      throw new TypeError('invalid_trace_owner')
    }

    return { owner: { ...owner }, uploadable: false }
  }

  if (
    owner.accountId === null ||
    owner.sessionId === null ||
    !isCanonicalUuidV4(owner.accountKey.slice('account-'.length)) ||
    !isCanonicalUuidV4(owner.accountId) ||
    !isCanonicalUuidV4(owner.sessionId) ||
    owner.accountKey !== `account-${owner.accountId}`
  ) {
    throw new TypeError('invalid_trace_owner')
  }

  return { owner: { ...owner }, uploadable: true }
}
