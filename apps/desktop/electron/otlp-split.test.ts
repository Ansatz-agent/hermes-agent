import assert from 'node:assert/strict'

import { test } from 'vitest'

import { deriveOtlpCorrelation } from './otlp-correlation'
import { splitOtlpExportTraceRequest } from './otlp-split'

function varint(value: number): Buffer {
  const bytes: number[] = []
  let remaining = value

  do {
    const byte = remaining & 0x7f

    remaining = Math.floor(remaining / 128)
    bytes.push(remaining > 0 ? byte | 0x80 : byte)
  } while (remaining > 0)

  return Buffer.from(bytes)
}

function lengthDelimited(field: number, value: Buffer | string): Buffer {
  const body = typeof value === 'string' ? Buffer.from(value) : value

  return Buffer.concat([varint((field << 3) | 2), varint(body.length), body])
}

function fields(message: Buffer): Array<{ body: Buffer; number: number }> {
  const result: Array<{ body: Buffer; number: number }> = []
  let offset = 0

  while (offset < message.length) {
    const key = readVarint(message, offset)

    assert.ok(key)
    offset = key.offset
    const length = readVarint(message, offset)

    assert.ok(length)
    offset = length.offset
    const end = offset + length.value

    assert.ok(end <= message.length)
    result.push({ body: message.subarray(offset, end), number: Math.floor(key.value / 8) })
    offset = end
  }

  return result
}

function readVarint(buffer: Buffer, start: number): { offset: number; value: number } | null {
  let multiplier = 1
  let value = 0

  for (let offset = start; offset < buffer.length && offset < start + 10; offset += 1) {
    const byte = buffer[offset]

    value += (byte & 0x7f) * multiplier

    if ((byte & 0x80) === 0) {
      return { offset: offset + 1, value }
    }

    multiplier *= 128
  }

  return null
}

function span(id: number, padding: number): Buffer {
  return Buffer.concat([
    lengthDelimited(1, Buffer.alloc(16, id)),
    lengthDelimited(2, Buffer.alloc(8, id)),
    lengthDelimited(5, Buffer.alloc(padding, id))
  ])
}

type ResourceInput = {
  resource: string
  scope: string
  spans: Buffer[]
}

function resourceSpans(input: ResourceInput): Buffer {
  return resourceSpansWithScopes(input.resource, [{ scope: input.scope, spans: input.spans }])
}

function resourceSpansWithScopes(resourceName: string, scopes: Array<{ scope: string; spans: Buffer[] }>): Buffer {
  const resource = lengthDelimited(100, resourceName)

  return Buffer.concat([
    lengthDelimited(97, ''),
    lengthDelimited(1, resource),
    ...scopes.map(({ scope, spans }) => {
      const instrumentationScope = lengthDelimited(1, scope)

      const scopedSpans = Buffer.concat([
        lengthDelimited(98, ''),
        lengthDelimited(1, instrumentationScope),
        ...spans.map(value => lengthDelimited(2, value)),
        lengthDelimited(99, '')
      ])

      return lengthDelimited(2, scopedSpans)
    }),
    lengthDelimited(98, '')
  ])
}

function exportRequests(resources: ResourceInput[]): Buffer {
  return exportResourceMessages(resources.map(resourceSpans))
}

function exportResourceMessages(resources: Buffer[]): Buffer {
  return Buffer.concat([
    lengthDelimited(97, ''),
    ...resources.map(resource => lengthDelimited(1, resource)),
    lengthDelimited(98, '')
  ])
}

function exportRequest(input: ResourceInput): Buffer {
  return exportRequests([input])
}

function readSpanIds(request: Buffer): number[] {
  const ids: number[] = []

  for (const resource of fields(request).filter(field => field.number === 1)) {
    for (const scope of fields(resource.body).filter(field => field.number === 2)) {
      for (const candidate of fields(scope.body).filter(field => field.number === 2)) {
        const traceId = fields(candidate.body).find(field => field.number === 1)?.body

        assert.ok(traceId)
        ids.push(traceId[0])
      }
    }
  }

  return ids
}

function readResource(request: Buffer): string {
  const resourceSpansMessage = fields(request).find(field => field.number === 1)?.body
  const resource = resourceSpansMessage && fields(resourceSpansMessage).find(field => field.number === 1)?.body
  const name = resource && fields(resource).find(field => field.number === 100)?.body

  assert.ok(name)

  return name.toString()
}

function readScope(request: Buffer): string {
  const resourceSpansMessage = fields(request).find(field => field.number === 1)?.body
  const scopeSpans = resourceSpansMessage && fields(resourceSpansMessage).find(field => field.number === 2)?.body
  const scope = scopeSpans && fields(scopeSpans).find(field => field.number === 1)?.body
  const name = scope && fields(scope).find(field => field.number === 1)?.body

  assert.ok(name)

  return name.toString()
}

test('splits by span while preserving resource and scope and quarantines one oversize span', () => {
  const first = span(1, 70)
  const second = span(2, 70)
  const body = exportRequest({ resource: 'service-a', scope: 'relay', spans: [first, second] })
  const result = splitOtlpExportTraceRequest(body, 150)

  assert.equal(result.batches.length, 2)
  assert.deepEqual(result.batches.flatMap(readSpanIds), [1, 2])
  assert.ok(result.batches.every(batch => readResource(batch) === 'service-a'))
  assert.ok(result.batches.every(batch => readScope(batch) === 'relay'))
  assert.deepEqual(result.batches, [
    exportRequest({ resource: 'service-a', scope: 'relay', spans: [first] }),
    exportRequest({ resource: 'service-a', scope: 'relay', spans: [second] })
  ])

  const third = span(3, 512)

  const oversize = splitOtlpExportTraceRequest(
    exportRequest({ resource: 'service-a', scope: 'relay', spans: [third] }),
    150
  )

  assert.equal(oversize.batches.length, 0)
  assert.deepEqual(oversize.oversizedSpans.flatMap(readSpanIds), [3])
  assert.deepEqual(oversize.oversizedSpans, [exportRequest({ resource: 'service-a', scope: 'relay', spans: [third] })])

  assert.ok([...result.batches, ...oversize.oversizedSpans].every(deriveOtlpCorrelation))
})

test('continues across later scopes and resources after an oversize span', () => {
  const body = exportResourceMessages([
    resourceSpansWithScopes('service-a', [
      { scope: 'relay-a', spans: [span(4, 16), span(5, 512)] },
      { scope: 'relay-a-later', spans: [span(6, 16)] }
    ]),
    resourceSpans({ resource: 'service-b', scope: 'relay-b', spans: [span(7, 16)] })
  ])

  const result = splitOtlpExportTraceRequest(body, 150)

  assert.deepEqual(result.batches.flatMap(readSpanIds), [4, 6, 7])
  assert.deepEqual(result.oversizedSpans.flatMap(readSpanIds), [5])
  assert.deepEqual(result.batches.map(readResource), ['service-a', 'service-a', 'service-b'])
  assert.deepEqual(result.batches.map(readScope), ['relay-a', 'relay-a-later', 'relay-b'])
  assert.ok([...result.batches, ...result.oversizedSpans].every(batch => batch.length > 0))
  assert.ok([...result.batches, ...result.oversizedSpans].every(deriveOtlpCorrelation))
})

test('rejects malformed protobuf deterministically', () => {
  const malformed = Buffer.from([0x0a, 0x05, 0x01])

  assert.deepEqual(splitOtlpExportTraceRequest(malformed, 150), {
    batches: [],
    oversizedSpans: [malformed]
  })
})
