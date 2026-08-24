import { encodeLengthDelimited, encodeMessage, type ProtobufField, readFields } from './otlp-correlation'

export type SplitOtlpResult = {
  batches: Buffer[]
  oversizedSpans: Buffer[]
}

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
    return { batches: [], oversizedSpans: [body] }
  }

  if (body.length <= maxBytes) {
    return { batches: [body], oversizedSpans: [] }
  }

  const batches: Buffer[] = []
  const oversizedSpans: Buffer[] = []

  if (decoded.resources.length === 0) {
    return { batches: [], oversizedSpans: [body] }
  }

  for (const resource of decoded.resources) {
    let current: EnvelopeUnit[] = []

    for (const unit of resource.units) {
      const candidate = encodeExport(decoded.fields, resource, [...current, unit])

      if (candidate.length <= maxBytes) {
        current.push(unit)

        continue
      }

      if (current.length > 0) {
        batches.push(encodeExport(decoded.fields, resource, current))
      }

      current = []
      const single = encodeExport(decoded.fields, resource, [unit])

      if (single.length > maxBytes) {
        oversizedSpans.push(single)

        continue
      }

      current.push(unit)
    }

    if (current.length > 0) {
      batches.push(encodeExport(decoded.fields, resource, current))
    }
  }

  return { batches, oversizedSpans }
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
