import { createHash, timingSafeEqual } from 'node:crypto'

import { type EncryptedTraceRecord } from './trace-outbox-crypto'
import { type DurableTraceBatch, type TraceOwner, validateTraceOwner } from './trace-outbox-types'

const RECORD_MAGIC = Buffer.from('ATOB', 'ascii')
const RECORD_VERSION = 1
const CHECKSUM_BYTES = 32
const PREFIX_BYTES = 25
const MAX_HEADER_BYTES = 64 * 1024
const MAX_CIPHERTEXT_BYTES = 64 * 1024 * 1024
const NONCE_BYTES = 12
const TAG_BYTES = 16
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const SHA256_HEX = /^[0-9a-f]{64}$/
const CORRELATION_ID = /^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/
const ENTRYPOINTS = new Set(['cli', 'dashboard', 'desktop', 'voice'])

const HEADER_FIELDS = new Set([
  'attempt',
  'batchId',
  'contentType',
  'createdAt',
  'entrypoint',
  'hermesSessionId',
  'lastErrorClass',
  'nextRetryAt',
  'owner',
  'payloadSha256',
  'runId',
  'sequence',
  'telemetrySchemaVersion'
])

const OWNER_FIELDS = new Set(['accountId', 'accountKey', 'installationId', 'sessionId'])

export type TraceSegmentHeader = Omit<DurableTraceBatch, 'body'>

export interface TraceSegmentRecord {
  header: TraceSegmentHeader
  encrypted: EncryptedTraceRecord
}

export interface DecodedTraceSegmentRecord extends TraceSegmentRecord {
  nextOffset: number
}

function checksum(bytes: Buffer): Buffer {
  return createHash('sha256').update(bytes).digest()
}

function isRecordObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function hasExactFields(value: Record<string, unknown>, expected: Set<string>): boolean {
  const fields = Object.keys(value)

  return fields.length === expected.size && fields.every(field => expected.has(field))
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function isTraceOwner(value: unknown): value is TraceOwner {
  if (
    !isRecordObject(value) ||
    !hasExactFields(value, OWNER_FIELDS) ||
    typeof value.accountKey !== 'string' ||
    !isNullableString(value.accountId) ||
    !isNullableString(value.sessionId) ||
    typeof value.installationId !== 'string'
  ) {
    return false
  }

  try {
    validateTraceOwner({
      accountKey: value.accountKey,
      accountId: value.accountId,
      sessionId: value.sessionId,
      installationId: value.installationId
    })

    return true
  } catch {
    return false
  }
}

function isTraceSegmentHeader(value: unknown): value is TraceSegmentHeader {
  if (!isRecordObject(value) || !hasExactFields(value, HEADER_FIELDS)) {
    return false
  }

  return (
    isNonNegativeSafeInteger(value.attempt) &&
    typeof value.batchId === 'string' &&
    UUID_V4.test(value.batchId) &&
    value.contentType === 'application/x-protobuf' &&
    isNonNegativeSafeInteger(value.createdAt) &&
    typeof value.entrypoint === 'string' &&
    ENTRYPOINTS.has(value.entrypoint) &&
    typeof value.hermesSessionId === 'string' &&
    CORRELATION_ID.test(value.hermesSessionId) &&
    (value.lastErrorClass === null ||
      (typeof value.lastErrorClass === 'string' && CORRELATION_ID.test(value.lastErrorClass))) &&
    isNonNegativeSafeInteger(value.nextRetryAt) &&
    isTraceOwner(value.owner) &&
    typeof value.payloadSha256 === 'string' &&
    SHA256_HEX.test(value.payloadSha256) &&
    typeof value.runId === 'string' &&
    CORRELATION_ID.test(value.runId) &&
    isNonNegativeSafeInteger(value.sequence) &&
    value.telemetrySchemaVersion === '1'
  )
}

function validateLengths(headerLength: number, nonceLength: number, tagLength: number, ciphertextLength: number): void {
  if (nonceLength !== NONCE_BYTES) {
    throw new RangeError('invalid_nonce_length')
  }

  if (tagLength !== TAG_BYTES) {
    throw new RangeError('invalid_tag_length')
  }

  if (headerLength > MAX_HEADER_BYTES) {
    throw new RangeError('header_too_large')
  }

  if (ciphertextLength > MAX_CIPHERTEXT_BYTES) {
    throw new RangeError('ciphertext_too_large')
  }
}

export function encodeSegmentRecord(record: TraceSegmentRecord): Buffer {
  const header = Buffer.from(JSON.stringify(record.header), 'utf8')
  const { nonce, ciphertext, tag, originalBytes } = record.encrypted

  validateLengths(header.length, nonce.length, tag.length, ciphertext.length)

  if (!Number.isSafeInteger(originalBytes) || originalBytes < 0 || originalBytes > 0xffffffff) {
    throw new RangeError('invalid_original_length')
  }

  const prefix = Buffer.alloc(PREFIX_BYTES)

  RECORD_MAGIC.copy(prefix, 0)
  prefix.writeUInt8(RECORD_VERSION, 4)
  prefix.writeUInt32BE(header.length, 5)
  prefix.writeUInt32BE(nonce.length, 9)
  prefix.writeUInt32BE(tag.length, 13)
  prefix.writeUInt32BE(ciphertext.length, 17)
  prefix.writeUInt32BE(originalBytes, 21)

  const recordBody = Buffer.concat([prefix, header, nonce, ciphertext, tag])

  return Buffer.concat([recordBody, checksum(recordBody)])
}

export function decodeSegmentRecord(segment: Buffer, offset: number): DecodedTraceSegmentRecord | null {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > segment.length) {
    throw new RangeError('invalid_record_offset')
  }

  if (segment.length - offset < PREFIX_BYTES) {
    return null
  }

  if (!segment.subarray(offset, offset + RECORD_MAGIC.length).equals(RECORD_MAGIC)) {
    throw new Error('invalid_record_magic')
  }

  if (segment.readUInt8(offset + 4) !== RECORD_VERSION) {
    throw new Error('unsupported_record_version')
  }

  const headerLength = segment.readUInt32BE(offset + 5)
  const nonceLength = segment.readUInt32BE(offset + 9)
  const tagLength = segment.readUInt32BE(offset + 13)
  const ciphertextLength = segment.readUInt32BE(offset + 17)
  const originalBytes = segment.readUInt32BE(offset + 21)

  validateLengths(headerLength, nonceLength, tagLength, ciphertextLength)

  const recordBytes = PREFIX_BYTES + headerLength + nonceLength + ciphertextLength + tagLength
  const nextOffset = offset + recordBytes + CHECKSUM_BYTES

  if (nextOffset > segment.length) {
    return null
  }

  const recordStart = offset
  const checksumStart = offset + recordBytes
  const expectedChecksum = checksum(segment.subarray(recordStart, checksumStart))
  const storedChecksum = segment.subarray(checksumStart, nextOffset)

  if (!timingSafeEqual(expectedChecksum, storedChecksum)) {
    throw new Error('invalid_record_checksum')
  }

  const headerStart = offset + PREFIX_BYTES
  const nonceStart = headerStart + headerLength
  const ciphertextStart = nonceStart + nonceLength
  const tagStart = ciphertextStart + ciphertextLength
  let header: unknown

  try {
    header = JSON.parse(segment.toString('utf8', headerStart, nonceStart))
  } catch {
    throw new Error('invalid_record_header')
  }

  if (!isTraceSegmentHeader(header)) {
    throw new Error('invalid_record_header')
  }

  return {
    header,
    encrypted: {
      nonce: Buffer.from(segment.subarray(nonceStart, ciphertextStart)),
      ciphertext: Buffer.from(segment.subarray(ciphertextStart, tagStart)),
      tag: Buffer.from(segment.subarray(tagStart, checksumStart)),
      originalBytes
    },
    nextOffset
  }
}
