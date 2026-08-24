const POLICY = {
  months: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
  retry: { baseMs: 1_000, capMs: 5 * 60 * 1_000 }
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const OBSOLETE_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

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

  const date = parseHttpDate(header, now)

  if (date === null || date - now > Number.MAX_SAFE_INTEGER) {
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

function parseHttpDate(value: string, now: number): number | null {
  const imf =
    /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat), (\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(
      value
    )

  const obsolete =
    /^(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday), (\d{2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2}) (\d{2}):(\d{2}):(\d{2}) GMT$/.exec(
      value
    )

  const asctime =
    /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ( {1,2}\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})$/.exec(
      value
    )

  if (imf) {
    return checkedHttpDate(
      imf[1],
      Number(imf[4]),
      imf[3],
      Number(imf[2]),
      Number(imf[5]),
      Number(imf[6]),
      Number(imf[7])
    )
  }

  if (obsolete) {
    const currentYear = new Date(now).getUTCFullYear()

    if (!Number.isSafeInteger(currentYear)) {
      return null
    }

    let year = Math.floor(currentYear / 100) * 100 + Number(obsolete[4])

    if (year - currentYear > 50) {
      year -= 100
    }

    return checkedHttpDate(
      obsolete[1],
      year,
      obsolete[3],
      Number(obsolete[2]),
      Number(obsolete[5]),
      Number(obsolete[6]),
      Number(obsolete[7])
    )
  }

  if (asctime) {
    return checkedHttpDate(
      asctime[1],
      Number(asctime[7]),
      asctime[2],
      Number(asctime[3].trim()),
      Number(asctime[4]),
      Number(asctime[5]),
      Number(asctime[6])
    )
  }

  return null
}

function checkedHttpDate(
  weekday: string,
  year: number,
  monthName: string,
  day: number,
  hour: number,
  minute: number,
  second: number
): number | null {
  const month = POLICY.months.indexOf(monthName)
  const expectedWeekday = WEEKDAYS.includes(weekday) ? weekday : WEEKDAYS[OBSOLETE_WEEKDAYS.indexOf(weekday)]

  if (
    month < 0 ||
    !Number.isSafeInteger(year) ||
    year < 100 ||
    day < 1 ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    hour < 0 ||
    minute < 0 ||
    second < 0
  ) {
    return null
  }

  const timestamp = Date.UTC(year, month, day, hour, minute, second)
  const date = new Date(timestamp)

  if (
    !Number.isSafeInteger(timestamp) ||
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month ||
    date.getUTCDate() !== day ||
    WEEKDAYS[date.getUTCDay()] !== expectedWeekday
  ) {
    return null
  }

  return timestamp
}
