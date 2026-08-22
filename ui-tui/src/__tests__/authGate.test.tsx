import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React, { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AuthGate, type TuiAuthStatus } from '../authGate.js'
import { stripAnsi } from '../lib/text.js'

class FakeInput extends EventEmitter {
  chunks: string[] = []
  isRaw = false
  isTTY = true
  readableLength = 0

  read() {
    const next = this.chunks.shift() ?? null

    this.readableLength = this.chunks.length

    return next
  }

  ref = vi.fn()
  setEncoding = vi.fn()
  setRawMode = vi.fn((enabled: boolean) => {
    this.isRaw = enabled
  })
  unref = vi.fn()

  send(...chunks: string[]) {
    this.chunks.push(...chunks)
    this.readableLength = this.chunks.length
    this.emit('readable')
  }
}

const signedOut: TuiAuthStatus = {
  epoch: 1,
  reason: 'signed_out',
  runtime_instance_id: '0123456789abcdef0123456789abcdef',
  session_expires_at: null,
  state: 'signed_out',
  username: null,
  valid_until: 0
}

const authenticated: TuiAuthStatus = {
  ...signedOut,
  epoch: 2,
  reason: null,
  state: 'authenticated',
  username: 'alice',
  valid_until: 9999
}

class FakeGateway extends EventEmitter {
  authLogin = vi.fn(async (_username: string, _password: string) => authenticated)
  authLogout = vi.fn(async () => signedOut)
  authStatus = vi.fn(async () => signedOut)
  drain = vi.fn()
}

function mount(gw: FakeGateway, child: React.ReactNode = null) {
  const stdin = new FakeInput()
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 90, isTTY: false, rows: 30 })
  Object.assign(stderr, { columns: 90, isTTY: false, rows: 30 })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(React.createElement(AuthGate, { gw }, child), {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  return {
    cleanup: () => {
      instance.unmount()
      instance.cleanup()
    },
    input: stdin,
    output: () => stripAnsi(output)
  }
}

const settle = (ms = 20) => new Promise(resolve => setTimeout(resolve, ms))

afterEach(() => vi.restoreAllMocks())

describe('Ink account hard gate', () => {
  it('shows only administrator-issued account login controls while signed out', async () => {
    const gw = new FakeGateway()
    const mounted = mount(gw)

    await settle()
    const output = mounted.output()

    expect(output).toContain('Sign in to Hermes')
    expect(output).toContain('Username')
    expect(output).toContain('Password')
    expect(output).toContain('Accounts are created by the server administrator')
    expect(output).not.toMatch(/register|sign up|reset password|server url|offline|skip/i)
    mounted.cleanup()
  })

  it('masks the password and submits it without printing it', async () => {
    const gw = new FakeGateway()
    const mounted = mount(gw)

    await settle()
    mounted.input.send('alice', '\r')
    await settle()
    mounted.input.send('correct horse')
    await settle()
    expect(mounted.output()).toContain('*************')
    mounted.input.send('\r')
    await vi.waitFor(() => expect(gw.authLogin).toHaveBeenCalledWith('alice', 'correct horse'))

    expect(mounted.output()).not.toContain('correct horse')
    mounted.cleanup()
  })

  it('mounts capabilities only for the matching authenticated scope and unmounts immediately on lock', async () => {
    const gw = new FakeGateway()
    gw.authStatus.mockResolvedValue(authenticated)
    const lifecycle: string[] = []

    function ProtectedApp() {
      useEffect(() => {
        lifecycle.push('mounted')

        return () => {
          lifecycle.push('unmounted')
        }
      }, [])

      return <Text>protected-agent-ui</Text>
    }

    const mounted = mount(gw, <ProtectedApp />)

    await vi.waitFor(() => expect(lifecycle, mounted.output()).toEqual(['mounted']))
    gw.emit('event', { type: 'auth.changed', payload: { ...signedOut, epoch: 3, state: 'locked' } })
    await vi.waitFor(() => expect(lifecycle).toEqual(['mounted', 'unmounted']))
    expect(mounted.output()).toContain('Sign in to Hermes')
    mounted.cleanup()
  })

  it('rejects an authenticated event from a different runtime owner', async () => {
    const gw = new FakeGateway()
    const lifecycle: string[] = []

    const mounted = mount(
      gw,
      React.createElement(() => {
        lifecycle.push('rendered')

        return <Text>protected-agent-ui</Text>
      })
    )

    await settle()
    gw.emit('event', {
      type: 'auth.changed',
      payload: { ...authenticated, runtime_instance_id: 'ffffffffffffffffffffffffffffffff' }
    })
    await settle()

    expect(lifecycle).toEqual([])
    expect(mounted.output()).toContain('Sign in to Hermes')
    mounted.cleanup()
  })
})
