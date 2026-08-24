import { Box, Text, useInput } from '@hermes/ink'
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'

import { TextInput } from './components/textInput.js'
import type { GatewayClient } from './gatewayClient.js'
import type { TuiAuthStatus as AuthStatus, GatewayEvent } from './gatewayTypes.js'

export type TuiAuthStatus = AuthStatus

const FIXED_SERVER = 'https://c2sml.cn/agent'

const checkingStatus: TuiAuthStatus = {
  epoch: 0,
  reason: null,
  runtime_instance_id: '',
  session_expires_at: null,
  state: 'checking',
  username: null,
  valid_until: 0
}

const reasonText: Record<Exclude<TuiAuthStatus['reason'], null>, string> = {
  interactive_login_required: 'Sign in with the account provided by your administrator.',
  invalid_credentials: 'The username or password is incorrect.',
  rate_limited: 'Too many attempts. Wait a moment, then retry.',
  runtime_unavailable: 'The account service is unavailable. Press Ctrl+R to retry.',
  server_unavailable: 'The account server cannot be reached. Press Ctrl+R to retry.',
  session_expired: 'Your login expired. Sign in again.',
  session_rejected: 'The server rejected this login. Sign in again.',
  signed_out: 'Sign in to continue.',
  vault_unavailable: 'Secure credential storage is unavailable. Contact the server administrator.'
}

function sameScope(left: TuiAuthStatus, right: TuiAuthStatus): boolean {
  return (
    left.state === 'authenticated' &&
    right.state === 'authenticated' &&
    left.runtime_instance_id === right.runtime_instance_id &&
    left.epoch === right.epoch
  )
}

function isAuthEvent(event: GatewayEvent): event is Extract<GatewayEvent, { type: 'auth.changed' | 'auth.status' }> {
  return event.type === 'auth.changed' || event.type === 'auth.status'
}

export function AuthGate({ children, gw }: { children?: ReactNode; gw: GatewayClient }) {
  const [status, setStatus] = useState<TuiAuthStatus>(checkingStatus)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [field, setField] = useState<'password' | 'username'>('username')
  const [submitting, setSubmitting] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const mounted = useRef(true)

  const applyLocked = useCallback((next: TuiAuthStatus) => {
    setStatus(next)
    setPassword('')
    setSubmitting(false)
    setField('username')
    setMessage(next.reason ? reasonText[next.reason] : null)
  }, [])

  const refresh = useCallback(async () => {
    setMessage(null)

    try {
      const next = await gw.authStatus()

      if (!mounted.current) {
        return
      }

      if (next.state === 'authenticated') {
        setStatus(next)
        setMessage(null)
      } else {
        applyLocked(next)
      }
    } catch {
      if (mounted.current) {
        applyLocked({ ...checkingStatus, reason: 'runtime_unavailable', state: 'locked' })
      }
    }
  }, [applyLocked, gw])

  useEffect(() => {
    mounted.current = true

    const onEvent = (event: GatewayEvent) => {
      if (!isAuthEvent(event)) {
        return
      }

      const next = event.payload

      if (next.state !== 'authenticated') {
        applyLocked(next)

        return
      }

      // An authenticated broadcast is only a hint. Re-read through the exact
      // request/response channel and require the full owner tuple to match.
      void gw
        .authStatus()
        .then(current => {
          if (mounted.current && sameScope(next, current)) {
            setStatus(current)
            setMessage(null)
          }
        })
        .catch(() => {
          if (mounted.current) {
            applyLocked({ ...checkingStatus, reason: 'runtime_unavailable', state: 'locked' })
          }
        })
    }

    gw.on('event', onEvent)
    gw.drain()
    void refresh()

    return () => {
      mounted.current = false
      gw.off('event', onEvent)
    }
  }, [applyLocked, gw, refresh])

  useInput(
    (input, key) => {
      if (key.ctrl && input.toLowerCase() === 'r') {
        void refresh()
      } else if (key.tab) {
        setField(current => (current === 'username' ? 'password' : 'username'))
      }
    },
    { isActive: status.state !== 'authenticated' }
  )

  const submit = async (submittedPassword: string) => {
    if (submitting || !username.trim() || !submittedPassword) {
      setMessage(!username.trim() ? 'Enter your username.' : 'Enter your password.')

      return
    }

    setSubmitting(true)
    setMessage(null)

    try {
      const next = await gw.authLogin(username.trim(), submittedPassword)

      setPassword('')

      if (next.state === 'authenticated') {
        setStatus(next)
        setMessage(null)
      } else {
        applyLocked(next)
      }
    } catch {
      setPassword('')
      setMessage('Sign-in failed. Check your credentials or press Ctrl+R to retry.')
    } finally {
      if (mounted.current) {
        setSubmitting(false)
      }
    }
  }

  if (status.state === 'authenticated') {
    return <>{children}</>
  }

  return (
    <Box flexDirection="column" paddingX={2} paddingY={1}>
      <Text bold color="cyan">
        Sign in to Hermes
      </Text>
      <Text color="gray">Server: {FIXED_SERVER}</Text>
      <Text color="gray">Accounts are created by the server administrator.</Text>

      <Box marginTop={1}>
        <Text color={field === 'username' ? 'cyan' : undefined}>Username: </Text>
        <TextInput
          columns={54}
          focus={field === 'username'}
          onChange={setUsername}
          onSubmit={() => setField('password')}
          placeholder="administrator-issued username"
          value={username}
        />
      </Box>

      <Box>
        <Text color={field === 'password' ? 'cyan' : undefined}>Password: </Text>
        <TextInput
          columns={54}
          focus={field === 'password'}
          mask="*"
          onChange={setPassword}
          onSubmit={value => void submit(value)}
          placeholder="password"
          value={password}
        />
      </Box>

      <Box marginTop={1}>
        <Text color={message ? 'yellow' : 'gray'}>
          {submitting ? 'Signing in…' : (message ?? 'Enter submits · Tab changes field · Ctrl+R retries')}
        </Text>
      </Box>
    </Box>
  )
}
