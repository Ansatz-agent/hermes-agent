import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type PropsWithChildren } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

const apiMocks = vi.hoisted(() => ({
  prepareSenseVoice: vi.fn()
}))

vi.mock('@/hermes', () => ({
  prepareSenseVoice: apiMocks.prepareSenseVoice
}))

import { useSenseVoiceReadiness } from './use-composer-voice'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function createWrapper(client: QueryClient) {
  return function wrapper({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, children)
  }
}

describe('useSenseVoiceReadiness', () => {
  beforeEach(() => {
    apiMocks.prepareSenseVoice.mockReset()
    $connection.set({ baseUrl: 'http://127.0.0.1:43210', connectionId: 'local-1', mode: 'local', profile: 'default' } as never)
  })

  afterEach(() => {
    $connection.set(null)
    vi.clearAllMocks()
  })

  it('prevents an older preparation response from overwriting a retry and polls through ready', async () => {
    const stale = deferred<{ code: 'download_failed'; state: 'error' }>()
    let pollCount = 0
    apiMocks.prepareSenseVoice.mockImplementation((retry: boolean) => {
      if (retry) {
        return Promise.resolve({ downloaded: 35_651_584, phase: 'download', state: 'preparing', total: 163_002_883 })
      }

      pollCount += 1

      return pollCount === 1 ? stale.promise : Promise.resolve({ state: 'ready' })
    })

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result } = renderHook(() => useSenseVoiceReadiness('sensevoice'), {
      wrapper: createWrapper(client)
    })

    await waitFor(() => {
      expect(apiMocks.prepareSenseVoice).toHaveBeenCalledWith(false)
    })

    await act(async () => {
      await result.current.retry()
    })
    expect(result.current.status).toMatchObject({ state: 'preparing' })

    await act(async () => {
      stale.resolve({ code: 'download_failed', state: 'error' })
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(result.current.status).toEqual({ state: 'ready' })
        expect(result.current.ready).toBe(true)
      },
      { timeout: 2_500 }
    )
  })
})
