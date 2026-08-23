#!/usr/bin/env node

import { pathToFileURL } from 'node:url'

import { CDP } from '../apps/desktop/scripts/perf/lib/cdp.mjs'

const LOOPBACK_HOST = '127.0.0.1'
const MAX_CREDENTIAL_FRAME_BYTES = 16 * 1024
const FORM_TIMEOUT_MS = 31 * 60_000
const PROGRESS_TIMEOUT_MS = 60_000
const RELOCK_TIMEOUT_MS = 30_000

const EXIT = Object.freeze({
  config: 64,
  cdp: 69,
  form: 70,
  auth: 71,
  progress: 72,
  protectedUi: 73,
  backend: 74,
  logout: 75,
  relock: 76
})

class DriverFailure extends Error {
  constructor(exitCode, stage) {
    super(stage)
    this.exitCode = exitCode
    this.stage = stage
  }
}

export function parseCredentialFrame(buffer) {
  const parts = buffer.toString('utf8').split('\0')

  if (parts.length !== 3 || parts[2] !== '' || !parts[0] || !parts[1]) {
    throw new Error('credential_frame_invalid')
  }

  return { username: parts[0], password: parts[1] }
}

function parseArgs(argv) {
  const values = new Map()

  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index]
    const value = argv[index + 1]

    if (!['--port', '--timeout-ms'].includes(flag) || value === undefined) {
      throw new DriverFailure(EXIT.config, 'config')
    }

    values.set(flag, value)
  }

  const port = Number(values.get('--port'))
  const timeoutMs = Number(values.get('--timeout-ms'))

  if (
    values.size !== 2 ||
    !Number.isInteger(port) ||
    port < 1024 ||
    port > 65535 ||
    !Number.isInteger(timeoutMs) ||
    timeoutMs < PROGRESS_TIMEOUT_MS ||
    timeoutMs > 60 * 60_000
  ) {
    throw new DriverFailure(EXIT.config, 'config')
  }

  return { port, timeoutMs }
}

async function readCredentialFrame() {
  const chunks = []
  let bytes = 0

  for await (const chunk of process.stdin) {
    bytes += chunk.length
    if (bytes > MAX_CREDENTIAL_FRAME_BYTES) {
      throw new DriverFailure(EXIT.config, 'config')
    }
    chunks.push(chunk)
  }

  try {
    return parseCredentialFrame(Buffer.concat(chunks))
  } catch {
    throw new DriverFailure(EXIT.config, 'config')
  }
}

function emit(stage, result, startedAt) {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
  process.stdout.write(`credential_driver_stage=${stage} result=${result} elapsed_seconds=${elapsedSeconds}\n`)
}

async function waitFor(cdp, predicateExpression, timeoutMs) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    if (await cdp.eval(predicateExpression)) {
      return true
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }

  return false
}

function inputExpression(username, password) {
  return `(() => {
    const usernameInput = document.querySelector('input[name="username"]')
    const passwordInput = document.querySelector('input[name="password"]')
    if (!(usernameInput instanceof HTMLInputElement) ||
        !(passwordInput instanceof HTMLInputElement)) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    if (!setter) return false
    setter.call(usernameInput, ${JSON.stringify(username)})
    usernameInput.dispatchEvent(new Event('input', { bubbles: true }))
    usernameInput.dispatchEvent(new Event('change', { bubbles: true }))
    setter.call(passwordInput, ${JSON.stringify(password)})
    passwordInput.dispatchEvent(new Event('input', { bubbles: true }))
    passwordInput.dispatchEvent(new Event('change', { bubbles: true }))
    return true
  })()`
}

async function run() {
  const startedAt = Date.now()
  const { port, timeoutMs } = parseArgs(process.argv.slice(2))
  const credentials = await readCredentialFrame()
  let cdp

  try {
    try {
      cdp = await CDP.connect({ host: LOOPBACK_HOST, port, timeoutMs: FORM_TIMEOUT_MS })
    } catch {
      throw new DriverFailure(EXIT.cdp, 'cdp')
    }

    const formReady = await waitFor(
      cdp,
      `Boolean(document.querySelector('input[name="username"]') &&
        document.querySelector('input[name="password"]') &&
        document.querySelector('form button[type="submit"]'))`,
      FORM_TIMEOUT_MS
    )

    if (!formReady) {
      throw new DriverFailure(EXIT.form, 'form')
    }
    emit('form', 'PASS', startedAt)

    let valuesApplied = false
    try {
      valuesApplied = await cdp.eval(inputExpression(credentials.username, credentials.password))
    } finally {
      credentials.username = ''
      credentials.password = ''
    }

    if (!valuesApplied) {
      throw new DriverFailure(EXIT.form, 'form')
    }

    const submitted = await cdp.eval(`(() => {
      const form = document.querySelector('input[name="password"]')?.form
      const button = form?.querySelector('button[type="submit"]')
      if (!(form instanceof HTMLFormElement) || !(button instanceof HTMLButtonElement) || button.disabled) return false
      form.requestSubmit()
      return true
    })()`)
    if (!submitted) {
      throw new DriverFailure(EXIT.form, 'form')
    }
    emit('submit', 'PASS', startedAt)

    const authenticated = await waitFor(
      cdp,
      `(async () => (await window.hermesDesktop.auth.status()).state === 'authenticated')()`,
      timeoutMs
    )
    if (!authenticated) {
      throw new DriverFailure(EXIT.auth, 'auth')
    }
    emit('auth', 'PASS', startedAt)

    const detailedProgress = await waitFor(
      cdp,
      `(() => {
        const text = document.body?.textContent || ''
        return text.includes('Account verified, preparing Hermes') &&
          Boolean(document.querySelector('[role="status"] [aria-label="Hermes runtime installation"]')) &&
          /\\d+ of \\d+ stages complete/.test(text) &&
          text.includes('Elapsed')
      })()`,
      PROGRESS_TIMEOUT_MS
    )
    if (!detailedProgress) {
      throw new DriverFailure(EXIT.progress, 'progress')
    }
    emit('progress', 'PASS', startedAt)

    const protectedUi = await waitFor(
      cdp,
      `Boolean(document.querySelector('[data-slot="statusbar"]') &&
        !document.querySelector('input[name="password"]'))`,
      timeoutMs
    )
    if (!protectedUi) {
      throw new DriverFailure(EXIT.protectedUi, 'protected_ui')
    }
    emit('protected_ui', 'PASS', startedAt)

    const backendReady = await waitFor(
      cdp,
      `(async () => {
        try {
          return Boolean(await window.hermesDesktop.getConnection())
        } catch {
          return false
        }
      })()`,
      60_000
    )
    if (!backendReady) {
      throw new DriverFailure(EXIT.backend, 'backend')
    }
    emit('backend', 'PASS', startedAt)

    const loggedOut = await cdp.eval(`(async () =>
      (await window.hermesDesktop.auth.logout()).state === 'signed_out')()`)
    if (!loggedOut) {
      throw new DriverFailure(EXIT.logout, 'logout')
    }
    emit('logout', 'PASS', startedAt)

    const relocked = await waitFor(
      cdp,
      `Boolean(document.querySelector('input[name="username"]') &&
        document.querySelector('input[name="password"]') &&
        !document.querySelector('[data-slot="statusbar"]'))`,
      RELOCK_TIMEOUT_MS
    )
    const protectedRejected = await cdp.eval(`(async () => {
      try {
        await window.hermesDesktop.getConnection()
        return false
      } catch (error) {
        return String(error).includes('AUTH_REQUIRED')
      }
    })()`)
    if (!relocked || !protectedRejected) {
      throw new DriverFailure(EXIT.relock, 'relock')
    }
    emit('relock', 'PASS', startedAt)
  } finally {
    credentials.username = ''
    credentials.password = ''
    cdp?.close()
  }
}

async function main() {
  const startedAt = Date.now()

  try {
    await run()
  } catch (error) {
    const failure = error instanceof DriverFailure ? error : new DriverFailure(EXIT.cdp, 'cdp')
    emit(failure.stage, 'FAIL', startedAt)
    process.exitCode = failure.exitCode
  }
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href

if (isMain) {
  await main()
}

export { EXIT, LOOPBACK_HOST }
