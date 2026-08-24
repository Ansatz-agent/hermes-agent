const POLICY = {
  httpDate: {
    asctime:
      /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ \d]\d \d{2}:\d{2}:\d{2} \d{4}$/,
    imfFixdate:
      /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT$/,
    rfc850:
      /^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), \d{2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2} \d{2}:\d{2}:\d{2} GMT$/
  },
  retry: { baseMs: 1_000, capMs: 5 * 60 * 1_000 }
}

export type TraceRetryOptions = {
  attempt: number
  now: number
  random: () => number
  retryAfterMs?: number | null
}

export function nextTraceRetry({ attempt, now, random, retryAfterMs }: TraceRetryOptions): number {
  if (!Number.isSafeInteger(attempt) || attempt < 0 || !Number.isSafeInteger(now)) {
    throw new TypeError('invalid_trace_retry')
  }

  const jitter = fullJitterDelay(attempt, random)
  const retryAfter = validDelay(retryAfterMs, now)
  const delay = retryAfter !== null && retryAfter > jitter ? retryAfter : jitter

  if (delay > Number.MAX_SAFE_INTEGER - now) {
    throw new TypeError('invalid_trace_retry')
  }

  return now + delay
}

export function parseRetryAfterMs(value: string | null | undefined, now: number): number | null {
  if (typeof value !== 'string' || !Number.isSafeInteger(now)) {
    return null
  }

  const header = value.trim()

  if (/^\d+$/.test(header)) {
    const seconds = Number(header)

    if (!Number.isSafeInteger(seconds) || seconds > Math.floor((Number.MAX_SAFE_INTEGER - now) / 1_000)) {
      return null
    }

    return seconds * 1_000
  }

  if (
    !POLICY.httpDate.imfFixdate.test(header) &&
    !POLICY.httpDate.rfc850.test(header) &&
    !POLICY.httpDate.asctime.test(header)
  ) {
    return null
  }

  const date = Date.parse(header)

  if (!Number.isSafeInteger(date) || date > Number.MAX_SAFE_INTEGER || date - now > Number.MAX_SAFE_INTEGER) {
    return null
  }

  return Math.max(0, date - now)
}

export const parseTraceRetryAfterMs = parseRetryAfterMs

function fullJitterDelay(attempt: number, random: () => number): number {
  const exponential = attempt >= 9 ? POLICY.retry.capMs : POLICY.retry.baseMs * 2 ** attempt
  const cap = Math.min(POLICY.retry.capMs, exponential)
  const sample = random()
  const normalized = Number.isFinite(sample) ? Math.min(1, Math.max(0, sample)) : 0

  return Math.floor(cap * normalized)
}

function validDelay(value: number | null | undefined, now: number): number | null {
  if (!Number.isSafeInteger(value) || value < 0 || value > Number.MAX_SAFE_INTEGER - now) {
    return null
  }

  return value
}
