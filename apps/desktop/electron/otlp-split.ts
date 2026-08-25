import { encodeLengthDelimited, encodeMessage, type ProtobufField, readFields } from './otlp-correlation'

export type SplitOtlpResult = {
  batches: Buffer[]
  oversizedSpans: Buffer[]
  parts: SplitOtlpPart[]
}

export type SplitOtlpPart = { body: Buffer; kind: 'batch' } | { body: Buffer; kind: 'oversized-span' }

type SpanUnit = {
  kind: 'span'
  scopeFieldIndex: number
  scopeFields: ProtobufField[]
  spanFieldIndex: number
}

type EmptyScopeUnit = {
  kind: 'empty-scope'
  scopeFieldIndex: number
}

type EmptyResourceUnit = {
  kind: 'empty-resource'
}

type EnvelopeUnit = EmptyResourceUnit | EmptyScopeUnit | SpanUnit

type ResourceEnvelope = {
  fieldIndex: number
  fields: ProtobufField[]
  units: EnvelopeUnit[]
}

type DecodedExportRequest = {
  fields: ProtobufField[]
  resources: ResourceEnvelope[]
}

export function splitOtlpExportTraceRequest(body: Buffer, maxBytes: number): SplitOtlpResult {
  const decoded = decodeExportRequest(body)

  if (!decoded || maxBytes <= 0) {
    return { batches: [], oversizedSpans: [body], parts: [{ body, kind: 'oversized-span' }] }
  }

  if (body.length <= maxBytes) {
    return { batches: [body], oversizedSpans: [], parts: [{ body, kind: 'batch' }] }
  }

  const batches: Buffer[] = []
  const oversizedSpans: Buffer[] = []
  const parts: SplitOtlpPart[] = []

  if (decoded.resources.length === 0) {
    return { batches: [], oversizedSpans: [body], parts: [{ body, kind: 'oversized-span' }] }
  }

  // Batch sizes are computed arithmetically from precomputed field lengths so
  // packing N spans costs O(N); a batch is only materialized once complete.
  const requestOverheadBytes = decoded.fields.reduce(
    (total, field) => (field.number !== 1 || field.wireType !== 2 ? total + field.encoded.length : total),
    0
  )

  for (const resource of decoded.resources) {
    const resourceTotalBytes = resource.fields.reduce((total, field) => total + field.encoded.length, 0)

    const resourceOverheadBytes = resource.fields.reduce(
      (total, field) => (field.number !== 2 || field.wireType !== 2 ? total + field.encoded.length : total),
      0
    )

    const scopeOverheadBytes = new Map<number, number>()

    const unitBytes = resource.units.map(unit => {
      if (unit.kind === 'empty-resource') {
        return 0
      }

      if (unit.kind === 'empty-scope') {
        return resource.fields[unit.scopeFieldIndex].encoded.length
      }

      let overhead = scopeOverheadBytes.get(unit.scopeFieldIndex)

      if (overhead === undefined) {
        overhead = unit.scopeFields.reduce(
          (total, field) => (field.number !== 2 || field.wireType !== 2 ? total + field.encoded.length : total),
          0
        )
        scopeOverheadBytes.set(unit.scopeFieldIndex, overhead)
      }

      const payload = overhead + unit.scopeFields[unit.spanFieldIndex].encoded.length

      return 1 + varintLength(payload) + payload
    })

    const batchBytes = (unitsPayloadBytes: number, emptyResource: boolean): number => {
      const payload = emptyResource ? resourceTotalBytes : resourceOverheadBytes + unitsPayloadBytes

      return requestOverheadBytes + 1 + varintLength(payload) + payload
    }

    let current: EnvelopeUnit[] = []
    let currentPayloadBytes = 0

    const flush = (): void => {
      if (current.length > 0) {
        const batch = encodeExport(decoded.fields, resource, current)

        batches.push(batch)
        parts.push({ body: batch, kind: 'batch' })
        current = []
        currentPayloadBytes = 0
      }
    }

    for (const [unitIndex, unit] of resource.units.entries()) {
      const emptyResource = unit.kind === 'empty-resource'

      if (batchBytes(currentPayloadBytes + unitBytes[unitIndex], emptyResource) <= maxBytes) {
        current.push(unit)
        currentPayloadBytes += unitBytes[unitIndex]

        continue
      }

      flush()

      if (batchBytes(unitBytes[unitIndex], emptyResource) > maxBytes) {
        const single = encodeExport(decoded.fields, resource, [unit])

        oversizedSpans.push(single)
        parts.push({ body: single, kind: 'oversized-span' })

        continue
      }

      current.push(unit)
      currentPayloadBytes = unitBytes[unitIndex]
    }

    flush()
  }

  return { batches, oversizedSpans, parts }
}

function varintLength(value: number): number {
  let length = 1
  let remaining = value

  while (remaining >= 0x80) {
    remaining = Math.floor(remaining / 0x80)
    length += 1
  }

  return length
}

function decodeExportRequest(body: Buffer): DecodedExportRequest | null {
  const fields = readFields(body)

  if (!fields) {
    return null
  }

  const resources: ResourceEnvelope[] = []

  for (const [fieldIndex, field] of fields.entries()) {
    if (field.number !== 1 || field.wireType !== 2 || !field.bytes) {
      continue
    }

    const resourceFields = readFields(field.bytes)

    if (!resourceFields || !validateNestedMessages(resourceFields, 1)) {
      return null
    }

    const resource: ResourceEnvelope = {
      fieldIndex,
      fields: resourceFields,
      units: []
    }

    for (const [scopeFieldIndex, scopeField] of resourceFields.entries()) {
      if (scopeField.number !== 2 || scopeField.wireType !== 2 || !scopeField.bytes) {
        continue
      }

      const scopeFields = readFields(scopeField.bytes)

      if (!scopeFields || !validateNestedMessages(scopeFields, 1)) {
        return null
      }

      let spanCount = 0

      for (const [spanFieldIndex, spanField] of scopeFields.entries()) {
        if (spanField.number !== 2 || spanField.wireType !== 2 || !spanField.bytes) {
          continue
        }

        if (!readFields(spanField.bytes)) {
          return null
        }

        spanCount += 1
        resource.units.push({
          kind: 'span',
          scopeFieldIndex,
          scopeFields,
          spanFieldIndex
        })
      }

      if (spanCount === 0) {
        resource.units.push({ kind: 'empty-scope', scopeFieldIndex })
      }
    }

    if (resource.units.length === 0) {
      resource.units.push({ kind: 'empty-resource' })
    }

    resources.push(resource)
  }

  return { fields, resources }
}

function validateNestedMessages(fields: ProtobufField[], fieldNumber: number): boolean {
  return fields.every(
    field => field.number !== fieldNumber || field.wireType !== 2 || !field.bytes || readFields(field.bytes) !== null
  )
}

function encodeExport(requestFields: ProtobufField[], resource: ResourceEnvelope, units: EnvelopeUnit[]): Buffer {
  const encodedResource = encodeResource(resource, units)
  const parts: Buffer[] = []

  for (const [fieldIndex, field] of requestFields.entries()) {
    if (field.number !== 1 || field.wireType !== 2) {
      parts.push(field.encoded)
    } else if (fieldIndex === resource.fieldIndex) {
      parts.push(encodeLengthDelimited(1, encodedResource))
    }
  }

  return Buffer.concat(parts)
}

function encodeResource(resource: ResourceEnvelope, units: EnvelopeUnit[]): Buffer {
  if (units.some(unit => unit.kind === 'empty-resource')) {
    return encodeMessage(resource.fields)
  }

  const parts: Buffer[] = []

  for (const [fieldIndex, field] of resource.fields.entries()) {
    if (field.number !== 2 || field.wireType !== 2) {
      parts.push(field.encoded)

      continue
    }

    for (const unit of units) {
      if (unit.kind === 'span' && unit.scopeFieldIndex === fieldIndex) {
        parts.push(encodeScope(unit))
      } else if (unit.kind === 'empty-scope' && unit.scopeFieldIndex === fieldIndex) {
        parts.push(field.encoded)
      }
    }
  }

  return Buffer.concat(parts)
}

function encodeScope(span: SpanUnit): Buffer {
  const fields = span.scopeFields.filter(
    (field, fieldIndex) => field.number !== 2 || field.wireType !== 2 || fieldIndex === span.spanFieldIndex
  )

  return encodeLengthDelimited(2, encodeMessage(fields))
}
