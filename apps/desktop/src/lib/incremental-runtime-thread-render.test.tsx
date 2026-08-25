import { AssistantRuntimeProvider, type AssistantRuntime, type ThreadMessage } from '@assistant-ui/react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useMemo, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Thread } from '@/components/assistant-ui/thread'

import { useIncrementalExternalStoreRuntime } from './incremental-external-store-runtime'

const createdAt = new Date('2026-05-01T00:00:00.000Z')

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)
vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
  window.setTimeout(() => callback(performance.now()), 0)
)
vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
vi.stubGlobal('CSS', { escape: (str: string) => str })

Element.prototype.scrollTo = function scrollTo() {}
Element.prototype.animate = function animate() {
  return { cancel: () => {}, finished: Promise.resolve() } as unknown as Animation
}

function userMessage(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'Stream a response' }],
    attachments: [],
    createdAt,
    metadata: { custom: {} }
  } as ThreadMessage
}

function assistantMessage(text: string, running = true): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [{ type: 'text', text }],
    status: running ? { type: 'running' } : { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: {}
    }
  } as unknown as ThreadMessage
}

function Harness({ onRuntime }: { onRuntime?: (runtime: AssistantRuntime) => void }) {
  const [messages, setMessages] = useState<ThreadMessage[]>([userMessage()])
  const [isRunning, setIsRunning] = useState(true)
  const repository = useMemo(
    () => ({
      headId: messages.at(-1)?.id ?? null,
      messages: messages.map((message, index) => ({
        message,
        parentId: index === 0 ? null : (messages[index - 1]?.id ?? null)
      }))
    }),
    [messages]
  )

  useEffect(() => {
    const first = window.setTimeout(() => {
      setMessages([userMessage(), assistantMessage('first chunk')])
    }, 20)
    const second = window.setTimeout(() => {
      setMessages([userMessage(), assistantMessage('first chunk second chunk')])
    }, 100)
    const complete = window.setTimeout(() => {
      setMessages([userMessage(), assistantMessage('first chunk second chunk', false)])
      setIsRunning(false)
    }, 140)

    return () => {
      window.clearTimeout(first)
      window.clearTimeout(second)
      window.clearTimeout(complete)
    }
  }, [])

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning,
    onNew: async () => {},
    onCancel: async () => {},
    onEdit: async () => {},
    onReload: async () => {}
  })
  onRuntime?.(runtime)

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

afterEach(() => cleanup())

describe('incremental runtime with the real thread renderer', () => {
  it('renders the first streamed response without a snapshot loop', async () => {
    const errors: unknown[] = []
    const errorSpy = vi.spyOn(console, 'error').mockImplementation((...args) => {
      errors.push(args)
    })

    let runtime!: AssistantRuntime
    render(<Harness onRuntime={value => (runtime = value)} />)
    expect(runtime.thread.getMessageByIndex(0)).toBe(runtime.thread.getMessageByIndex(0))
    expect(runtime.thread.getMessageById('user-1')).toBe(runtime.thread.getMessageById('user-1'))
    const messageRuntime = runtime.thread.getMessageById('user-1')
    expect(messageRuntime.getState()).toBe(messageRuntime.getState())
    expect(messageRuntime.composer.getState()).toBe(messageRuntime.composer.getState())
    expect(runtime.thread.getState()).toBe(runtime.thread.getState())
    expect(runtime.thread.composer.getState()).toBe(runtime.thread.composer.getState())
    await waitFor(() => {
      expect(screen.getByText('first chunk')).toBeTruthy()
    })

    errorSpy.mockRestore()
    expect(
      errors.filter(error => String(error).includes('getSnapshot') || String(error).includes('Maximum update depth'))
    ).toEqual([])
  })
})
