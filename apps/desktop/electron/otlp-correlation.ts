const CORRELATION_ID = /^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$/
const UTF8 = new TextDecoder('utf-8', { fatal: true })

export type ProtobufField = {
  bytes?: Buffer
  encoded: Buffer
  number: number
  wireType: number
}

export type OtlpCorrelation = {
  runId: string
  sessionId: string
}

const SESSION_KEYS = Object.freeze([
  'hermes.session.id',
  'session.id',
  'langfuse.session.id',
  'gen_ai.conversation.id',
  'nemo_relay.scope.metadata.session_id',
  'nemo_relay.mark.metadata.session_id',
  'nemo_relay.mark.data.session_id',
  'nemo_relay.scope.data.session_id',
  'nemo_relay.session.instance_id'
])

const RUN_KEYS = Object.freeze([
  'hermes.run.id',
  'run.id',
  'nemo_relay.scope.metadata.turn_id',
  'nemo_relay.mark.metadata.turn_id',
  'nemo_relay.scope.metadata.task_id'
])

/**
 * Extract trusted correlation input from an OTLP ExportTraceServiceRequest.
 *
 * NeMo Relay's native OTLP exporter supports static headers only. It emits
 * Hermes session/turn metadata as span attributes, so the Electron-owned
 * broker derives the Gateway headers from the authenticated protobuf body.
 * The first trace id is the stable fallback (and default run id) for payloads
 * whose span projection does not include a Hermes-specific attribute.
 */
export function deriveOtlpCorrelation(body: Buffer): OtlpCorrelation | null {
  const request = readFields(body)

  if (!request) {
    return null
  }

  const attributes = new Map<string, string>()
  let traceId = ''

  for (const resourceSpans of messages(request, 1)) {
    const resourceSpansFields = readFields(resourceSpans)

    if (!resourceSpansFields) {
      return null
    }

    for (const resource of messages(resourceSpansFields, 1)) {
      const resourceFields = readFields(resource)

      if (!resourceFields || !collectKeyValues(resourceFields, 1, attributes)) {
        return null
      }
    }

    for (const scopeSpans of messages(resourceSpansFields, 2)) {
      const scopeSpansFields = readFields(scopeSpans)

      if (!scopeSpansFields) {
        return null
      }

      for (const span of messages(scopeSpansFields, 2)) {
        const spanFields = readFields(span)

        if (!spanFields) {
          return null
        }

        if (!traceId) {
          const candidate = spanFields.find(field => field.number === 1 && field.wireType === 2)?.bytes

          if (candidate?.length === 16) {
            traceId = candidate.toString('hex')
          }
        }

        if (!collectKeyValues(spanFields, 9, attributes)) {
          return null
        }
      }
    }
  }

  if (!CORRELATION_ID.test(traceId)) {
    return null
  }

  return {
    runId: firstIdentifier(attributes, RUN_KEYS) ?? traceId,
    sessionId: firstIdentifier(attributes, SESSION_KEYS) ?? traceId
  }
}

function collectKeyValues(source: ProtobufField[], fieldNumber: number, target: Map<string, string>): boolean {
  for (const message of messages(source, fieldNumber)) {
    const keyValueFields = readFields(message)

    if (!keyValueFields) {
      return false
    }

    const keyBytes = keyValueFields.find(field => field.number === 1 && field.wireType === 2)?.bytes
    const valueMessage = keyValueFields.find(field => field.number === 2 && field.wireType === 2)?.bytes

    if (!keyBytes || !valueMessage) {
      continue
    }

    const valueFields = readFields(valueMessage)
    const valueBytes = valueFields?.find(field => field.number === 1 && field.wireType === 2)?.bytes

    if (!valueFields || !valueBytes) {
      continue
    }

    try {
      const key = UTF8.decode(keyBytes)
      const value = UTF8.decode(valueBytes)

      if (!target.has(key)) {
        target.set(key, value)
      }
    } catch {
      return false
    }
  }

  return true
}

function firstIdentifier(attributes: Map<string, string>, keys: readonly string[]): string | null {
  for (const key of keys) {
    const value = attributes.get(key)

    if (value && CORRELATION_ID.test(value)) {
      return value
    }
  }

  return null
}

function messages(source: ProtobufField[], fieldNumber: number): Buffer[] {
  return source
    .filter(field => field.number === fieldNumber && field.wireType === 2 && field.bytes)
    .map(field => field.bytes!)
}

export function readFields(message: Buffer): ProtobufField[] | null {
  const sequence = readFieldSequence(message, 0, null)

  return sequence?.offset === message.length ? sequence.fields : null
}

function readFieldSequence(
  message: Buffer,
  startOffset: number,
  endGroupNumber: number | null
): { fields: ProtobufField[]; offset: number } | null {
  const result: ProtobufField[] = []
  let offset = startOffset

  while (offset < message.length) {
    const start = offset
    const key = readVarint(message, offset)

    if (!key || key.value === 0) {
      return null
    }

    offset = key.offset
    const number = Math.floor(key.value / 8)
    const wireType = key.value & 7

    if (number < 1) {
      return null
    }

    if (wireType === 4) {
      if (endGroupNumber === null || number !== endGroupNumber) {
        return null
      }

      return { fields: result, offset }
    }

    if (wireType === 3) {
      const group = readFieldSequence(message, offset, number)

      if (!group) {
        return null
      }

      offset = group.offset
      result.push({ encoded: message.subarray(start, offset), number, wireType })

      continue
    }

    if (wireType === 0) {
      const value = readVarint(message, offset)

      if (!value) {
        return null
      }

      offset = value.offset
      result.push({ encoded: message.subarray(start, offset), number, wireType })

      continue
    }

    if (wireType === 1 || wireType === 5) {
      const size = wireType === 1 ? 8 : 4

      if (offset + size > message.length) {
        return null
      }

      offset += size
      result.push({ encoded: message.subarray(start, offset), number, wireType })

      continue
    }

    if (wireType !== 2) {
      return null
    }

    const length = readVarint(message, offset)

    if (!length || length.value < 0 || length.offset + length.value > message.length) {
      return null
    }

    offset = length.offset
    const end = offset + length.value

    offset = end
    result.push({
      bytes: message.subarray(length.offset, end),
      encoded: message.subarray(start, offset),
      number,
      wireType
    })
  }

  return endGroupNumber === null ? { fields: result, offset } : null
}

export function encodeLengthDelimited(fieldNumber: number, value: Buffer): Buffer {
  return Buffer.concat([encodeVarint((fieldNumber << 3) | 2), encodeVarint(value.length), value])
}

export function encodeMessage(fields: readonly ProtobufField[]): Buffer {
  return Buffer.concat(fields.map(field => field.encoded))
}

function encodeVarint(value: number): Buffer {
  const bytes: number[] = []
  let remaining = value

  do {
    const byte = remaining & 0x7f

    remaining = Math.floor(remaining / 128)
    bytes.push(remaining > 0 ? byte | 0x80 : byte)
  } while (remaining > 0)

  return Buffer.from(bytes)
}

function readVarint(buffer: Buffer, start: number): { offset: number; value: number } | null {
  let multiplier = 1
  let value = 0

  for (let offset = start; offset < buffer.length && offset < start + 10; offset += 1) {
    const byte = buffer[offset]

    value += (byte & 0x7f) * multiplier

    if (!Number.isSafeInteger(value)) {
      return null
    }

    if ((byte & 0x80) === 0) {
      return { offset: offset + 1, value }
    }

    multiplier *= 128
  }

  return null
}
