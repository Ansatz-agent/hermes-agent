import { describe, expect, it } from 'vitest'

import type { DesktopSafeBootstrapState } from '@/global'

import {
  deriveAuthBootstrapProgress,
  formatBootstrapAmount,
  formatBootstrapElapsed,
  progressFraction,
  sanitizeAuthBootstrapText
} from './auth-bootstrap-progress'

const state: DesktopSafeBootstrapState = {
  active: true,
  manifest: {
    type: 'manifest',
    protocolVersion: 1,
    bootstrapScope: 'runtime',
    stages: [
      { name: 'prepare', title: 'Prepare runtime' },
      { name: 'python-deps', title: 'Install Python dependencies' },
      { name: 'complete', title: 'Finish install' }
    ]
  },
  stages: {
    prepare: { state: 'succeeded', durationMs: 1_000, startedAt: 500, error: null, progress: null },
    'python-deps': {
      state: 'running',
      durationMs: null,
      startedAt: 4_000,
      error: null,
      progress: {
        stage: 'python-deps',
        completed: 38_200_000,
        total: 126_500_000,
        unit: 'bytes',
        label: 'Hermes Python dependencies',
        updatedAt: 5_000
      }
    },
    complete: { state: 'pending', durationMs: null, startedAt: null, error: null, progress: null }
  },
  error: null,
  failedStage: null,
  startedAt: 100,
  completedAt: null
}

describe('auth bootstrap progress helpers', () => {
  it('derives exact stage counts, the current stage, and its elapsed time', () => {
    const progress = deriveAuthBootstrapProgress(state, 100_000)

    expect(progress.completedStages).toBe(1)
    expect(progress.totalStages).toBe(3)
    expect(progress.overallFraction).toBeCloseTo(1 / 3)
    expect(progress.current?.descriptor.name).toBe('python-deps')
    expect(progress.current?.elapsedMs).toBe(96_000)
    expect(progress.failed).toBeNull()
  })

  it('formats elapsed time without inventing sub-second precision', () => {
    expect(formatBootstrapElapsed(900)).toBe('0s')
    expect(formatBootstrapElapsed(59_900)).toBe('59s')
    expect(formatBootstrapElapsed(96_000)).toBe('1:36')
    expect(formatBootstrapElapsed(3_661_000)).toBe('1:01:01')
  })

  it('formats byte amounts and computes a fraction only from a real total', () => {
    expect(formatBootstrapAmount(38_200_000, 'bytes')).toBe('38.2 MB')
    expect(formatBootstrapAmount(126_500_000, 'bytes')).toBe('126.5 MB')
    expect(progressFraction({ completed: 38_200_000, total: 126_500_000 })).toBeCloseTo(0.301976)
    expect(progressFraction({ completed: 47, total: null })).toBeNull()
    expect(progressFraction({ completed: 47, total: 0 })).toBeNull()
  })

  it('selects the explicit failed stage instead of guessing from time', () => {
    const failed = deriveAuthBootstrapProgress(
      {
        ...state,
        active: false,
        error: 'bootstrap_failed',
        failedStage: 'python-deps',
        stages: {
          ...state.stages,
          'python-deps': { ...state.stages['python-deps'], state: 'failed', error: 'bootstrap_failed' }
        }
      },
      100_000
    )

    expect(failed.current).toBeNull()
    expect(failed.failed?.descriptor.name).toBe('python-deps')
  })

  it('rejects secret-like labels, paths, URLs, commands, and control characters', () => {
    expect(sanitizeAuthBootstrapText('Install Python dependencies', 'Safe fallback')).toBe(
      'Install Python dependencies'
    )

    for (const hostile of [
      'password=secret',
      'Cookie: abc',
      'Cookie hidden-value',
      'Session hidden-value',
      'sessionid=hidden',
      '/Users/alice/private',
      'C:\\Users\\alice\\private',
      'https://evil.invalid/path',
      'curl -H Authorization:secret',
      'safe\u0000password=secret'
    ]) {
      expect(sanitizeAuthBootstrapText(hostile, 'Safe fallback')).toBe('Safe fallback')
    }
  })
})
