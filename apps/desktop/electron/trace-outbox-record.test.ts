import assert from 'node:assert/strict'

import { test } from 'vitest'

import { encryptTraceRecord } from './trace-outbox-crypto'
import { decodeSegmentRecord, encodeSegmentRecord, type TraceSegmentHeader } from './trace-outbox-record'

function validHeader(): TraceSegmentHeader {
  return {
    attempt: 0,
    batchId: '44444444-4444-4444-8444-444444444444',
    contentType: 'application/x-protobuf',
    createdAt: 1_798_000_000_000,
    entrypoint: 'desktop',
    hermesSessionId: 'hermes-session-1',
    lastErrorClass: null,
    nextRetryAt: 0,
    owner: {
      accountKey: 'account-11111111-1111-4111-8111-111111111111',
      accountId: '11111111-1111-4111-8111-111111111111',
      sessionId: '22222222-2222-4222-8222-222222222222',
      installationId: '33333333-3333-4333-8333-333333333333'
    },
    payloadSha256: 'a'.repeat(64),
    runId: 'run-1',
    sequence: 7,
    telemetrySchemaVersion: '1'
  }
}

async function validRecord() {
  const encrypted = await encryptTraceRecord(
    Buffer.from('repeated history '.repeat(1_000)),
    Buffer.alloc(32, 7),
    Buffer.from('account-a/batch-1')
  )

  return { encrypted, header: validHeader() }
}

test('round trips a complete record and treats an incomplete append as a torn tail', async () => {
  const input = await validRecord()
  const encoded = encodeSegmentRecord(input)
  const decoded = decodeSegmentRecord(encoded, 0)

  assert.ok(decoded)
  assert.equal(decoded.nextOffset, encoded.length)
  assert.deepEqual(decoded.header, input.header)
  assert.deepEqual(decoded.encrypted, input.encrypted)
  assert.equal(decodeSegmentRecord(encoded.subarray(0, encoded.length - 1), 0), null)
})

test('decodes consecutive records from a caller supplied offset', async () => {
  const input = await validRecord()
  const first = encodeSegmentRecord(input)
  const second = encodeSegmentRecord({ ...input, header: { ...input.header, sequence: 8 } })
  const segment = Buffer.concat([first, second])
  const decodedFirst = decodeSegmentRecord(segment, 0)

  assert.ok(decodedFirst)
  const decodedSecond = decodeSegmentRecord(segment, decodedFirst.nextOffset)
  assert.ok(decodedSecond)
  assert.equal(decodedSecond.header.sequence, 8)
  assert.equal(decodedSecond.nextOffset, segment.length)
})

test('rejects a corrupt checksum, unknown version, and invalid magic', async () => {
  const encoded = encodeSegmentRecord(await validRecord())
  const badChecksum = Buffer.from(encoded)
  const badVersion = Buffer.from(encoded)
  const badMagic = Buffer.from(encoded)

  badChecksum[badChecksum.length - 1] ^= 1
  badVersion[4] = 2
  badMagic.write('NOPE', 0, 'ascii')

  assert.throws(() => decodeSegmentRecord(badChecksum, 0), /invalid_record_checksum/)
  assert.throws(() => decodeSegmentRecord(badVersion, 0), /unsupported_record_version/)
  assert.throws(() => decodeSegmentRecord(badMagic, 0), /invalid_record_magic/)
})

test('rejects invalid nonce, tag, header, and ciphertext lengths', async () => {
  const input = await validRecord()

  assert.throws(
    () => encodeSegmentRecord({ ...input, encrypted: { ...input.encrypted, nonce: Buffer.alloc(11) } }),
    /invalid_nonce_length/
  )
  assert.throws(
    () => encodeSegmentRecord({ ...input, encrypted: { ...input.encrypted, tag: Buffer.alloc(15) } }),
    /invalid_tag_length/
  )
  assert.throws(
    () => encodeSegmentRecord({ ...input, header: { ...input.header, runId: 'x'.repeat(65_536) } }),
    /header_too_large/
  )

  const encoded = encodeSegmentRecord(input)
  const oversizedCiphertextLength = Buffer.from(encoded)
  const invalidNonceLength = Buffer.from(encoded)
  const invalidTagLength = Buffer.from(encoded)

  oversizedCiphertextLength.writeUInt32BE(64 * 1024 * 1024 + 1, 17)
  invalidNonceLength.writeUInt32BE(11, 9)
  invalidTagLength.writeUInt32BE(15, 13)

  assert.throws(() => decodeSegmentRecord(oversizedCiphertextLength, 0), /ciphertext_too_large/)
  assert.throws(() => decodeSegmentRecord(invalidNonceLength, 0), /invalid_nonce_length/)
  assert.throws(() => decodeSegmentRecord(invalidTagLength, 0), /invalid_tag_length/)
})

test('rejects checksummed headers with missing or malformed owner, batch, and sequence fields', async () => {
  const input = await validRecord()

  const invalidHeaders: unknown[] = [
    {},
    { ...input.header, owner: null },
    { ...input.header, owner: { ...input.header.owner, accountKey: '../escape' } },
    { ...input.header, owner: { ...input.header.owner, unexpected: 'secret' } },
    { ...input.header, body: 'plaintext-must-not-enter-the-header' },
    { ...input.header, batchId: undefined },
    { ...input.header, batchId: 'not-a-uuid' },
    { ...input.header, sequence: undefined },
    { ...input.header, sequence: -1 },
    { ...input.header, sequence: 1.5 }
  ]

  for (const header of invalidHeaders) {
    const encoded = encodeSegmentRecord({ ...input, header: header as TraceSegmentHeader })

    assert.throws(() => decodeSegmentRecord(encoded, 0), /invalid_record_header/)
  }
})

test('rejects checksummed headers with invalid durable metadata formats and bounds', async () => {
  const input = await validRecord()

  const invalidHeaders: TraceSegmentHeader[] = [
    { ...input.header, attempt: -1 },
    { ...input.header, createdAt: -1 },
    { ...input.header, nextRetryAt: -1 },
    { ...input.header, payloadSha256: 'A'.repeat(64) },
    { ...input.header, contentType: 'text/plain' as TraceSegmentHeader['contentType'] },
    { ...input.header, entrypoint: 'unknown' as TraceSegmentHeader['entrypoint'] },
    { ...input.header, hermesSessionId: '../escape' },
    { ...input.header, runId: 'x'.repeat(129) },
    { ...input.header, telemetrySchemaVersion: '2' },
    { ...input.header, lastErrorClass: 'x'.repeat(129) }
  ]

  for (const header of invalidHeaders) {
    const encoded = encodeSegmentRecord({ ...input, header })

    assert.throws(() => decodeSegmentRecord(encoded, 0), /invalid_record_header/)
  }
})

test('rejects checksummed headers containing UUIDv4 prefixes followed by trailing text', async () => {
  const input = await validRecord()

  const invalidHeaders: TraceSegmentHeader[] = [
    { ...input.header, batchId: `${input.header.batchId}-suffix` },
    {
      ...input.header,
      owner: { ...input.header.owner, sessionId: `${input.header.owner.sessionId!}-suffix` }
    },
    {
      ...input.header,
      owner: { ...input.header.owner, installationId: `${input.header.owner.installationId}-suffix` }
    }
  ]

  for (const header of invalidHeaders) {
    const encoded = encodeSegmentRecord({ ...input, header })

    assert.throws(() => decodeSegmentRecord(encoded, 0), /invalid_record_header/)
  }
})
