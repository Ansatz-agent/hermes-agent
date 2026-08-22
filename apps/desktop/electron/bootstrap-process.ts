import { spawn } from 'node:child_process'

import {
  type BootstrapProgress,
  createBootstrapState,
  normalizeBootstrapProgress,
  reduceBootstrapState
} from './bootstrap-progress'

const DEFAULT_CAPTURE_LIMIT_BYTES = 256 * 1024
const DEFAULT_HARD_TIMEOUT_MS = 10 * 60_000
const DEFAULT_IDLE_TIMEOUT_MS = 90_000
const DEFAULT_KILL_GRACE_MS = 2_000
const MAX_EMITTED_LINE_CHARS = 64 * 1024
const STRUCTURED_PROGRESS_PREFIX = 'HERMES_BOOTSTRAP_PROGRESS '

export const DOMESTIC_BOOTSTRAP_MIRRORS = Object.freeze({
  pythonPrimary: 'https://mirrors.ustc.edu.cn/pypi/simple',
  pythonFallback: 'https://pypi.tuna.tsinghua.edu.cn/simple',
  npmRegistry: 'https://registry.npmmirror.com',
  nodeBase: 'https://registry.npmmirror.com/-/binary/node/',
  playwrightBase: 'https://registry.npmmirror.com/-/binary/playwright'
})

const SAFE_ENV_KEYS = new Set([
  'APPDATA',
  'DBUS_SESSION_BUS_ADDRESS',
  'DISPLAY',
  'HOME',
  'LANG',
  'LANGUAGE',
  'LC_ALL',
  'LC_CTYPE',
  'LOCALAPPDATA',
  'LOGNAME',
  'PATH',
  'SHELL',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'USER',
  'USERPROFILE',
  'WAYLAND_DISPLAY',
  'WINDIR',
  'XDG_CACHE_HOME',
  'XDG_CONFIG_HOME',
  'XDG_DATA_HOME',
  'XDG_RUNTIME_DIR'
])

export type BootstrapTermination = 'cancelled' | 'hard-timeout' | 'idle-timeout'

export type BootstrapProcessResult = {
  code: number | null
  killed: boolean
  signal: NodeJS.Signals | null
  stderr: string
  stdout: string
  termination: BootstrapTermination | null
}

type BuildBootstrapEnvironmentOptions = {
  hermesHome?: string
  npmConfigPath?: string
  npmRegistry?: string
  useDomesticRuntimeMirrors?: boolean
  uvIndexUrl?: string
}

type RunBootstrapProcessOptions = {
  abortSignal?: AbortSignal
  args?: string[]
  captureLimitBytes?: number
  command: string
  cwd?: string
  emit?: (event: BootstrapProcessEvent) => void
  env?: NodeJS.ProcessEnv
  hardTimeoutMs?: number
  idleTimeoutMs?: number
  killGraceMs?: number
  progressHeartbeatMs?: number
  stageName?: string
}

type BootstrapProcessEvent =
  | { line: string; stage?: string; stream: 'stderr' | 'stdout'; type: 'log' }
  | (BootstrapProgress & { type: 'progress' })

export function buildBootstrapEnvironment(
  source: NodeJS.ProcessEnv = process.env,
  options: BuildBootstrapEnvironmentOptions = {}
): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}

  for (const key of SAFE_ENV_KEYS) {
    const value = source[key]

    if (typeof value === 'string') {
      env[key] = value
    }
  }

  const nullConfig = process.platform === 'win32' ? 'NUL' : '/dev/null'

  env.HERMES_HOME = options.hermesHome || ''
  env.UV_NO_CONFIG = '1'
  env.PIP_CONFIG_FILE = nullConfig
  env.PIP_DISABLE_PIP_VERSION_CHECK = '1'
  env.PIP_NO_INPUT = '1'
  env.NPM_CONFIG_USERCONFIG = options.npmConfigPath || nullConfig
  env.NPM_CONFIG_FUND = 'false'
  env.NPM_CONFIG_AUDIT = 'false'
  env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
  env.CI = '1'

  if (options.uvIndexUrl) {
    env.UV_DEFAULT_INDEX = options.uvIndexUrl
  }

  if (options.npmRegistry) {
    env.NPM_CONFIG_REGISTRY = options.npmRegistry
  }

  if (options.useDomesticRuntimeMirrors) {
    env.UV_DEFAULT_INDEX = DOMESTIC_BOOTSTRAP_MIRRORS.pythonPrimary
    env.HERMES_UV_FALLBACK_INDEX = DOMESTIC_BOOTSTRAP_MIRRORS.pythonFallback
    env.NPM_CONFIG_REGISTRY = DOMESTIC_BOOTSTRAP_MIRRORS.npmRegistry
    env.HERMES_NODE_MIRROR = DOMESTIC_BOOTSTRAP_MIRRORS.nodeBase
    env.PLAYWRIGHT_DOWNLOAD_HOST = DOMESTIC_BOOTSTRAP_MIRRORS.playwrightBase
  }

  return env
}

function appendBoundedTail(existing: string, chunk: Buffer, limit: number): string {
  const combined = Buffer.concat([Buffer.from(existing), chunk])
  const bounded = combined.length > limit ? combined.subarray(combined.length - limit) : combined

  return bounded.toString('utf8')
}

function redactBootstrapLine(line: string): string {
  return line
    .replace(/((?:authorization|cookie|set-cookie)\s*:\s*)\S+/gi, '$1[REDACTED]')
    .replace(/((?:password|passwd|session|sessionid|csrf|csrftoken|bearer|keychain)\s*[=:]\s*)\S+/gi, '$1[REDACTED]')
}

export function runBootstrapProcess(options: RunBootstrapProcessOptions): Promise<BootstrapProcessResult> {
  const {
    abortSignal,
    args = [],
    captureLimitBytes = DEFAULT_CAPTURE_LIMIT_BYTES,
    command,
    cwd,
    emit,
    env = buildBootstrapEnvironment(),
    hardTimeoutMs = DEFAULT_HARD_TIMEOUT_MS,
    idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS,
    killGraceMs = DEFAULT_KILL_GRACE_MS,
    progressHeartbeatMs,
    stageName
  } = options

  if (abortSignal?.aborted) {
    return Promise.resolve({
      code: null,
      killed: true,
      signal: null,
      stderr: '',
      stdout: '',
      termination: 'cancelled'
    })
  }

  return new Promise<BootstrapProcessResult>((resolve, reject) => {
    const detached = process.platform !== 'win32'

    const child = spawn(command, args, {
      cwd,
      detached,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true
    })

    let stdout = ''
    let stderr = ''
    let stdoutLine = ''
    let stderrLine = ''
    let termination: BootstrapTermination | null = null
    let settled = false
    let killTimer: ReturnType<typeof setTimeout> | null = null
    let idleTimer: ReturnType<typeof setTimeout> | null = null
    let progressTimer: ReturnType<typeof setInterval> | null = null

    const parseStructuredProgress = (line: string): BootstrapProcessEvent | null => {
      if (!line.startsWith(STRUCTURED_PROGRESS_PREFIX) || !stageName) {
        return null
      }

      try {
        const parsed = JSON.parse(line.slice(STRUCTURED_PROGRESS_PREFIX.length))

        if (!parsed || parsed.type !== 'progress') {
          return null
        }

        const manifestState = reduceBootstrapState(
          createBootstrapState(),
          {
            type: 'manifest',
            protocolVersion: 1,
            bootstrapScope: 'runtime',
            stages: [{ name: stageName, title: stageName }]
          },
          Date.now()
        )

        const progress = normalizeBootstrapProgress(parsed, manifestState, Date.now())

        return progress ? { type: 'progress', ...progress } : null
      } catch {
        return null
      }
    }

    const emitLine = (line: string, stream: 'stderr' | 'stdout') => {
      if (!line || !emit) {
        return
      }

      if (line.startsWith(STRUCTURED_PROGRESS_PREFIX)) {
        const progress = parseStructuredProgress(line)

        if (progress) {
          emit(progress)
        }

        // Reserved frames are machine data. Invalid frames fail closed and
        // never become pre-auth renderer text.
        return
      }

      emit({ type: 'log', stage: stageName, line: redactBootstrapLine(line), stream })
    }

    const consumeLines = (chunk: string, stream: 'stderr' | 'stdout') => {
      let buffer = (stream === 'stdout' ? stdoutLine : stderrLine) + chunk
      let newline = buffer.indexOf('\n')

      while (newline >= 0) {
        emitLine(buffer.slice(0, newline).replace(/\r$/, '').slice(-MAX_EMITTED_LINE_CHARS), stream)
        buffer = buffer.slice(newline + 1)
        newline = buffer.indexOf('\n')
      }

      buffer = buffer.slice(-MAX_EMITTED_LINE_CHARS)

      if (stream === 'stdout') {
        stdoutLine = buffer
      } else {
        stderrLine = buffer
      }
    }

    const signalProcessTree = (signal: NodeJS.Signals) => {
      try {
        if (detached && child.pid) {
          process.kill(-child.pid, signal)
        } else {
          child.kill(signal)
        }
      } catch {
        try {
          child.kill(signal)
        } catch {
          // The child may have exited between the deadline and the signal.
        }
      }
    }

    const requestTermination = (reason: BootstrapTermination) => {
      if (termination || settled) {
        return
      }

      termination = reason

      if (progressTimer) {
        clearInterval(progressTimer)
        progressTimer = null
      }

      signalProcessTree('SIGTERM')
      killTimer = setTimeout(() => signalProcessTree('SIGKILL'), Math.max(0, killGraceMs))
    }

    const resetIdleTimer = () => {
      if (idleTimer) {
        clearTimeout(idleTimer)
      }

      idleTimer = setTimeout(() => requestTermination('idle-timeout'), Math.max(1, idleTimeoutMs))
    }

    const hardTimer = setTimeout(() => requestTermination('hard-timeout'), Math.max(1, hardTimeoutMs))
    const onAbort = () => requestTermination('cancelled')

    abortSignal?.addEventListener('abort', onAbort, { once: true })
    resetIdleTimer()

    if (progressHeartbeatMs && progressHeartbeatMs > 0 && stageName) {
      progressTimer = setInterval(() => {
        if (settled || termination) {
          return
        }

        emit?.({
          type: 'progress',
          stage: stageName,
          completed: 0,
          total: null,
          unit: 'items',
          label: stageName,
          updatedAt: Date.now()
        })
        resetIdleTimer()
      }, Math.max(1, progressHeartbeatMs))
    }

    child.stdout.on('data', (value: Buffer | string) => {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value)

      stdout = appendBoundedTail(stdout, chunk, captureLimitBytes)
      consumeLines(chunk.toString('utf8'), 'stdout')
      resetIdleTimer()
    })
    child.stderr.on('data', (value: Buffer | string) => {
      const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value)

      stderr = appendBoundedTail(stderr, chunk, captureLimitBytes)
      consumeLines(chunk.toString('utf8'), 'stderr')
      resetIdleTimer()
    })

    child.once('error', error => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(hardTimer)

      if (idleTimer) {clearTimeout(idleTimer)}

      if (killTimer) {clearTimeout(killTimer)}

      if (progressTimer) {clearInterval(progressTimer)}

      abortSignal?.removeEventListener('abort', onAbort)
      reject(error)
    })

    child.once('close', (code, signal) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(hardTimer)

      if (idleTimer) {clearTimeout(idleTimer)}

      if (killTimer) {clearTimeout(killTimer)}

      if (progressTimer) {clearInterval(progressTimer)}

      abortSignal?.removeEventListener('abort', onAbort)
      emitLine(stdoutLine, 'stdout')
      emitLine(stderrLine, 'stderr')
      resolve({ code, killed: termination !== null, signal, stderr, stdout, termination })
    })
  })
}
