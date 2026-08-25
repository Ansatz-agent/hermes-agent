import assert from 'node:assert/strict'

import { test } from 'vitest'

import { nextTraceRetry, parseRetryAfterMs } from './trace-retry-policy'

test('uses full-jitter exponential delay capped at five minutes', () => {
  assert.equal(nextTraceRetry({ attempt: 0, now: 10, random: () => 0.5 }), 510)
  assert.equal(nextTraceRetry({ attempt: 20, now: 10, random: () => 1 }), 300_010)
})

test('uses a longer valid Retry-After in preference to jitter', () => {
  assert.equal(nextTraceRetry({ attempt: 1, now: 10, random: () => 0, retryAfterMs: 600_000 }), 600_010)
  assert.equal(nextTraceRetry({ attempt: 3, now: 10, random: () => 1, retryAfterMs: 1_000 }), 8_010)
})

test('parses non-negative Retry-After seconds without accepting overflow or junk', () => {
  assert.equal(parseRetryAfterMs('0', 10), 0)
  assert.equal(parseRetryAfterMs(' 120 ', 10), 120_000)
  assert.equal(parseRetryAfterMs('-1', 10), null)
  assert.equal(parseRetryAfterMs('1.5', 10), null)
  assert.equal(parseRetryAfterMs('1e3', 10), null)
  assert.equal(parseRetryAfterMs('9007199254740992', 10), null)
})

test('parses RFC 7231 HTTP dates and leaves expired dates shorter than jitter', () => {
  const now = Date.parse('2026-08-25T00:00:00Z')

  assert.equal(parseRetryAfterMs('Tue, 25 Aug 2026 00:02:00 GMT', now), 120_000)
  assert.equal(parseRetryAfterMs('Mon, 24 Aug 2026 23:59:59 GMT', now), 0)
  assert.equal(parseRetryAfterMs('not a date', now), null)
  assert.equal(parseRetryAfterMs('Tue, 25 Aug 2026 00:02:00 PST', now), null)
})

test('parses every RFC 7231 HTTP-date form with the RFC850 century pivot', () => {
  const imfNow = Date.UTC(1994, 10, 6, 8, 49, 37)

  assert.equal(parseRetryAfterMs('Sun, 06 Nov 1994 08:50:37 GMT', imfNow), 60_000)
  assert.equal(parseRetryAfterMs('Sun Nov  6 08:50:37 1994', imfNow), 60_000)
  assert.equal(
    parseRetryAfterMs('Tuesday, 01-Jan-75 00:00:00 GMT', Date.UTC(2025, 0, 1)),
    Date.UTC(2075, 0, 1) - Date.UTC(2025, 0, 1)
  )
  assert.equal(
    parseRetryAfterMs('Wednesday, 01-Jan-75 00:00:00 GMT', Date.UTC(2024, 0, 1)),
    Math.max(0, Date.UTC(1975, 0, 1) - Date.UTC(2024, 0, 1))
  )
})

test('rejects HTTP dates with invalid calendar values, weekday, or timezone', () => {
  const now = Date.UTC(2026, 0, 1)

  assert.equal(parseRetryAfterMs('Tue, 31 Feb 2026 00:00:00 GMT', now), null)
  assert.equal(parseRetryAfterMs('Mon, 01 Jan 2026 00:00:00 GMT', now), null)
  assert.equal(parseRetryAfterMs('Thu Jan  1 00:00:00 2026 GMT', now), null)
})
