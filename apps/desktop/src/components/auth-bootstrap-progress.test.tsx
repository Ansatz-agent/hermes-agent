import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { DesktopSafeBootstrapState } from '@/global'
import { I18nProvider } from '@/i18n'

import { AuthBootstrapProgress } from './auth-bootstrap-progress'

function runtimeState(total: number | null, unit: 'bytes' | 'packages'): DesktopSafeBootstrapState {
  return {
    active: true,
    manifest: {
      type: 'manifest',
      protocolVersion: 1,
      bootstrapScope: 'runtime',
      stages: [
        { name: 'prepare', title: 'Prepare runtime' },
        { name: 'python-deps', title: 'Install Hermes Python dependencies' },
        { name: 'browser', title: 'Install browser components' },
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
          completed: unit === 'bytes' ? 38_200_000 : 47,
          total,
          unit,
          label: 'Hermes Python dependencies',
          updatedAt: 5_000
        }
      },
      browser: { state: 'pending', durationMs: null, startedAt: null, error: null, progress: null },
      complete: { state: 'pending', durationMs: null, startedAt: null, error: null, progress: null }
    },
    error: null,
    failedStage: null,
    startedAt: 100,
    completedAt: null
  }
}

function renderPanel(state: DesktopSafeBootstrapState, mode: 'auth' | 'runtime' = 'runtime') {
  const onLogout = vi.fn()
  const onRetry = vi.fn()

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <AuthBootstrapProgress mode={mode} now={100_000} onLogout={onLogout} onRetry={onRetry} state={state} />
    </I18nProvider>
  )

  return { onLogout, onRetry }
}

describe('AuthBootstrapProgress', () => {
  it('shows the full runtime stage list, exact overall count, and active elapsed time', () => {
    renderPanel(runtimeState(126_500_000, 'bytes'))

    expect(screen.getByText('1 of 4 stages complete')).not.toBeNull()
    expect(screen.getByText('Prepare runtime')).not.toBeNull()
    expect(screen.getByText('Install Hermes Python dependencies')).not.toBeNull()
    expect(screen.getByText('Install browser components')).not.toBeNull()
    expect(screen.getByText('Finish install')).not.toBeNull()
    expect(screen.getByText('Running · 1:36')).not.toBeNull()
    expect(screen.getByText('Completed')).not.toBeNull()
    expect(screen.getAllByText('Waiting').length).toBe(2)
    expect(screen.getByRole('button', { name: 'Sign out' })).not.toBeNull()
  })

  it('shows a real byte percentage when the total is known', () => {
    renderPanel(runtimeState(126_500_000, 'bytes'))

    expect(screen.getByText('38.2 MB / 126.5 MB, 30%')).not.toBeNull()
    expect(screen.getByRole('progressbar', { name: 'Hermes Python dependencies' }).getAttribute('aria-valuenow')).toBe(
      '30'
    )
  })

  it('uses indeterminate progress without a fabricated percentage when total is unknown', () => {
    renderPanel(runtimeState(null, 'packages'))

    expect(screen.getByText('47 packages')).not.toBeNull()
    expect(screen.queryByText(/47%/)).toBeNull()
    expect(
      screen.getByRole('progressbar', { name: 'Hermes Python dependencies' }).getAttribute('aria-valuenow')
    ).toBeNull()
  })

  it('shows auth preparation as a locked current-stage surface without Sign out', () => {
    const state = runtimeState(null, 'packages')
    state.manifest = { ...state.manifest!, bootstrapScope: 'auth' }

    renderPanel(state, 'auth')

    expect(screen.getByText('Preparing the secure sign-in service')).not.toBeNull()
    expect(screen.getByText('Stage 2 of 4: Install Hermes Python dependencies')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('shows only a safe failed stage and enables Retry on explicit failure', () => {
    const state = runtimeState(null, 'packages')
    state.active = false
    state.error = 'bootstrap_failed'
    state.failedStage = 'python-deps'
    state.stages['python-deps'] = {
      ...state.stages['python-deps'],
      state: 'failed',
      error: 'bootstrap_failed'
    }

    renderPanel(state)

    expect(screen.getByText('Failed at: Install Hermes Python dependencies')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Retry' })).not.toBeNull()
    expect(globalThis.document.body.textContent).not.toContain('Traceback')
    expect(globalThis.document.body.textContent).not.toContain('sessionid')
  })
})
