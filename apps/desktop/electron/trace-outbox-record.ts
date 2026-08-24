import { createHash, timingSafeEqual } from 'node:crypto'

import { type EncryptedTraceRecord } from './trace-outbox-crypto'
import { type DurableTraceBatch } from './trace-outbox-types'

const RECORD_MAGIC = Buffer.from('ATOB', 'ascii')
const RECORD_VERSION = 1
const CHECKSUM_BYTES = 32
const PREFIX_BYTES = 25
const MAX_HEADER_BYTES = 64 * 1024
const MAX_CIPHERTEXT_BYTES = 64 * 1024 * 1024
const NONCE_BYTES = 12
const TAG_BYTES = 16

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

  if (header === null || typeof header !== 'object' || Array.isArray(header)) {
    throw new Error('invalid_record_header')
  }

  return {
    header: header as TraceSegmentHeader,
    encrypted: {
      nonce: Buffer.from(segment.subarray(nonceStart, ciphertextStart)),
      ciphertext: Buffer.from(segment.subarray(ciphertextStart, tagStart)),
      tag: Buffer.from(segment.subarray(tagStart, checksumStart)),
      originalBytes
    },
    nextOffset
  }
}
