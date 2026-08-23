export type BootstrapScope = 'auth' | 'runtime'
export type BootstrapProgressUnit = 'bytes' | 'packages' | 'items' | 'files' | 'steps'
export type BootstrapStageState = 'pending' | 'running' | 'succeeded' | 'skipped' | 'failed'

export interface BootstrapStageDescriptor {
  name: string
  title?: string
  category?: string
  needs_user_input?: boolean
}

export interface BootstrapProgress {
  stage: string
  completed: number
  total: number | null
  unit: BootstrapProgressUnit
  label: string
  updatedAt: number
}

export interface BootstrapStageResult {
  state: BootstrapStageState
  durationMs: number | null
  startedAt: number | null
  json: { ok: boolean; skipped?: boolean; reason?: string | null; stage: string } | null
  error: string | null
  progress: BootstrapProgress | null
}

export interface BootstrapState {
  active: boolean
  manifest: {
    type: 'manifest'
    stages: BootstrapStageDescriptor[]
    protocolVersion: number | null
    bootstrapScope?: BootstrapScope
  } | null
  stages: Record<string, BootstrapStageResult>
  error: string | null
  failedStage: string | null
  log: Array<{ ts: number; stage: string | null; line: string; stream?: 'stdout' | 'stderr' }>
  startedAt: number | null
  completedAt: number | null
  setupChoice: { platform: string; activeRoot: string } | null
  unsupportedPlatform: { platform: string; activeRoot: string; installCommand: string; docsUrl: string } | null
}

export type BootstrapEvent =
  | { type: 'dismissed' }
  | { type: 'setup-choice'; active: boolean; platform?: string; activeRoot?: string }
  | {
      type: 'manifest'
      stages: BootstrapStageDescriptor[]
      protocolVersion: number | null
      bootstrapScope?: BootstrapScope
    }
  | {
      type: 'stage'
      name: string
      state: BootstrapStageState
      durationMs?: number
      json?: BootstrapStageResult['json']
      error?: string | null
    }
  | {
      type: 'progress'
      stage: string
      completed: number
      total: number | null
      unit: string
      label?: string
    }
  | { type: 'log'; stage?: string | null; line: string; stream?: 'stdout' | 'stderr' }
  | { type: 'complete'; marker?: Record<string, unknown> }
  | { type: 'failed'; stage?: string | null; error: string }
  | { type: 'unsupported-platform'; platform: string; activeRoot: string; installCommand: string; docsUrl: string }

export interface SafeBootstrapStageResult {
  state: BootstrapStageState
  durationMs: number | null
  startedAt: number | null
  error: 'bootstrap_failed' | null
  progress: BootstrapProgress | null
}

export interface SafeBootstrapState {
  active: boolean
  manifest: {
    type: 'manifest'
    stages: BootstrapStageDescriptor[]
    protocolVersion: number | null
    bootstrapScope?: BootstrapScope
  } | null
  stages: Record<string, SafeBootstrapStageResult>
  error: 'bootstrap_failed' | null
  failedStage: string | null
  startedAt: number | null
  completedAt: number | null
}

export type SafeBootstrapEvent =
  | { type: 'dismissed' }
  | SafeBootstrapState['manifest']
  | {
      type: 'stage'
      name: string
      state: BootstrapStageState
      durationMs?: number
      error?: 'bootstrap_failed'
    }
  | Omit<BootstrapProgress, 'updatedAt'> & { type: 'progress'; updatedAt: number }
  | { type: 'complete'; completedAt: number }
  | { type: 'failed'; stage?: string; error: 'bootstrap_failed' }

const PROGRESS_UNITS = new Set<BootstrapProgressUnit>(['bytes', 'packages', 'items', 'files', 'steps'])
const SAFE_STAGE_NAME = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/
const SENSITIVE_TERM = /\b(?:authorization|bearer|cookie|set-cookie|password|passwd|session|sessionid|csrf|csrftoken|keychain)\b/i
const LOCAL_OR_REMOTE_PATH = /(?:[a-z]:\\|\/(?:Users|home|private|var|tmp)\/|(?:https?|file):\/\/)/i
const UNSAFE_LABEL_CHARACTERS = /[^/\p{L}\p{N} .,_:;()\-+%]/gu
const MAX_LABEL_LENGTH = 80
const MAX_LOG_LINES = 500

function stripControlCharacters(value: string): string {
  return Array.from(value, character => {
    const code = character.charCodeAt(0)

    return code < 0x20 || (code >= 0x7f && code <= 0x9f) ? ' ' : character
  }).join('')
}

export function createBootstrapState(): BootstrapState {
  return {
    active: false,
    manifest: null,
    stages: {},
    error: null,
    failedStage: null,
    log: [],
    startedAt: null,
    completedAt: null,
    setupChoice: null,
    unsupportedPlatform: null
  }
}

export function safeBootstrapIdentifier(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null
  }

  const normalized = value.trim().toLowerCase()

  return SAFE_STAGE_NAME.test(normalized) ? normalized : null
}

export function sanitizeBootstrapLabel(value: unknown, fallback: string): string {
  const clean = (candidate: unknown) => {
    if (typeof candidate !== 'string') {
      return ''
    }

    const normalized = stripControlCharacters(candidate.normalize('NFKC'))
      .replace(/\s+/g, ' ')
      .trim()

    if (!normalized || SENSITIVE_TERM.test(normalized) || LOCAL_OR_REMOTE_PATH.test(normalized)) {
      return ''
    }

    return normalized.replace(UNSAFE_LABEL_CHARACTERS, '').trim().slice(0, MAX_LABEL_LENGTH)
  }

  return clean(value) || clean(fallback) || 'Hermes installation'
}

function safeTimestamp(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback
}

function safeCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function safeDuration(value: unknown): number | null {
  const duration = safeCount(value)

  return duration === null ? null : duration
}

function descriptorFor(state: BootstrapState, stageName: string): BootstrapStageDescriptor | null {
  return state.manifest?.stages.find(stage => stage.name === stageName) ?? null
}

export function normalizeBootstrapProgress(
  event: Extract<BootstrapEvent, { type: 'progress' }>,
  state: BootstrapState,
  now = Date.now()
): BootstrapProgress | null {
  const stage = safeBootstrapIdentifier(event.stage)
  const descriptor = stage ? descriptorFor(state, stage) : null
  const completed = safeCount(event.completed)
  const total = event.total === null ? null : safeCount(event.total)

  const unit = PROGRESS_UNITS.has(event.unit as BootstrapProgressUnit)
    ? (event.unit as BootstrapProgressUnit)
    : null

  if (!stage || !descriptor || completed === null || unit === null || (total !== null && total <= 0)) {
    return null
  }

  const boundedCompleted = total === null ? completed : Math.min(completed, total)

  return {
    stage,
    completed: boundedCompleted,
    total,
    unit,
    label: sanitizeBootstrapLabel(event.label, descriptor.title || descriptor.name),
    updatedAt: safeTimestamp(now, Date.now())
  }
}

export function reduceBootstrapState(
  state: BootstrapState,
  event: BootstrapEvent,
  now = Date.now()
): BootstrapState {
  const timestamp = safeTimestamp(now, Date.now())

  if (event.type === 'dismissed') {
    return createBootstrapState()
  }

  if (event.type === 'manifest') {
    const stages: Record<string, BootstrapStageResult> = {}

    for (const descriptor of event.stages) {
      const name = safeBootstrapIdentifier(descriptor.name)

      if (!name || stages[name]) {
        continue
      }

      stages[name] = {
        state: 'pending',
        durationMs: null,
        startedAt: null,
        json: null,
        error: null,
        progress: null
      }
    }

    return {
      ...state,
      active: true,
      manifest: {
        type: 'manifest',
        stages: event.stages.filter(descriptor => Boolean(safeBootstrapIdentifier(descriptor.name))),
        protocolVersion: safeCount(event.protocolVersion),
        ...(event.bootstrapScope ? { bootstrapScope: event.bootstrapScope } : {})
      },
      stages,
      error: null,
      failedStage: null,
      log: [],
      startedAt: timestamp,
      completedAt: null,
      setupChoice: null,
      unsupportedPlatform: null
    }
  }

  if (event.type === 'stage') {
    const name = safeBootstrapIdentifier(event.name)
    const previous = name ? state.stages[name] : null

    if (!name || !previous) {
      return state
    }

    const startedAt = event.state === 'running' ? (previous.startedAt ?? timestamp) : previous.startedAt

    return {
      ...state,
      stages: {
        ...state.stages,
        [name]: {
          ...previous,
          state: event.state,
          durationMs: safeDuration(event.durationMs),
          startedAt,
          json: event.json ?? null,
          error: event.error ?? null
        }
      },
      failedStage: event.state === 'failed' ? name : state.failedStage
    }
  }

  if (event.type === 'progress') {
    const progress = normalizeBootstrapProgress(event, state, timestamp)

    if (!progress) {
      return state
    }

    return {
      ...state,
      stages: {
        ...state.stages,
        [progress.stage]: { ...state.stages[progress.stage], progress }
      }
    }
  }

  if (event.type === 'log') {
    const log = state.log.concat({
      ts: timestamp,
      stage: safeBootstrapIdentifier(event.stage) || null,
      line: String(event.line),
      stream: event.stream || 'stdout'
    })

    return { ...state, log: log.slice(-MAX_LOG_LINES) }
  }

  if (event.type === 'complete') {
    return {
      ...state,
      active: false,
      error: null,
      failedStage: null,
      completedAt: timestamp,
      unsupportedPlatform: null
    }
  }

  if (event.type === 'failed') {
    const failedStage = safeBootstrapIdentifier(event.stage)

    return {
      ...state,
      active: false,
      error: event.error || 'unknown error',
      failedStage: failedStage && state.stages[failedStage] ? failedStage : state.failedStage,
      setupChoice: null
    }
  }

  if (event.type === 'unsupported-platform') {
    return {
      ...state,
      active: false,
      error: 'unsupported-platform',
      setupChoice: null,
      unsupportedPlatform: {
        platform: event.platform,
        activeRoot: event.activeRoot,
        installCommand: event.installCommand,
        docsUrl: event.docsUrl
      }
    }
  }

  if (event.type === 'setup-choice') {
    return {
      ...state,
      active: false,
      manifest: null,
      stages: {},
      error: null,
      failedStage: null,
      setupChoice: event.active
        ? { platform: String(event.platform || 'unknown'), activeRoot: String(event.activeRoot || '') }
        : null,
      unsupportedPlatform: null
    }
  }

  return state
}

function safeDescriptor(descriptor: BootstrapStageDescriptor): BootstrapStageDescriptor | null {
  const name = safeBootstrapIdentifier(descriptor.name)

  if (!name) {
    return null
  }

  return {
    name,
    title: sanitizeBootstrapLabel(descriptor.title, name),
    category: sanitizeBootstrapLabel(descriptor.category, 'runtime').toLowerCase(),
    needs_user_input: Boolean(descriptor.needs_user_input)
  }
}

export function safeBootstrapState(state: BootstrapState): SafeBootstrapState {
  const descriptors = (state.manifest?.stages || [])
    .map(safeDescriptor)
    .filter((descriptor): descriptor is BootstrapStageDescriptor => descriptor !== null)

  const stages: Record<string, SafeBootstrapStageResult> = {}

  for (const descriptor of descriptors) {
    const result = state.stages[descriptor.name]

    if (!result) {
      continue
    }

    stages[descriptor.name] = {
      state: result.state,
      durationMs: safeDuration(result.durationMs),
      startedAt: result.startedAt,
      error: result.state === 'failed' ? 'bootstrap_failed' : null,
      progress: result.progress
        ? {
            ...result.progress,
            label: sanitizeBootstrapLabel(result.progress.label, descriptor.title || descriptor.name)
          }
        : null
    }
  }

  const failedStage = safeBootstrapIdentifier(state.failedStage)

  return {
    active: state.active,
    manifest: state.manifest
      ? {
          type: 'manifest',
          stages: descriptors,
          protocolVersion: state.manifest.protocolVersion,
          ...(state.manifest.bootstrapScope ? { bootstrapScope: state.manifest.bootstrapScope } : {})
        }
      : null,
    stages,
    error: state.error ? 'bootstrap_failed' : null,
    failedStage: failedStage && stages[failedStage] ? failedStage : null,
    startedAt: state.startedAt,
    completedAt: state.completedAt
  }
}

export function safeBootstrapEvent(
  event: BootstrapEvent,
  state: BootstrapState,
  now = Date.now()
): SafeBootstrapEvent | null {
  if (event.type === 'log' || event.type === 'setup-choice') {
    return null
  }

  if (event.type === 'dismissed') {
    return event
  }

  if (event.type === 'manifest') {
    return safeBootstrapState(reduceBootstrapState(state, event, now)).manifest
  }

  if (event.type === 'stage') {
    const name = safeBootstrapIdentifier(event.name)

    if (!name || !state.stages[name]) {
      return null
    }

    return {
      type: 'stage',
      name,
      state: event.state,
      ...(safeDuration(event.durationMs) === null ? {} : { durationMs: safeDuration(event.durationMs)! }),
      ...(event.state === 'failed' ? { error: 'bootstrap_failed' as const } : {})
    }
  }

  if (event.type === 'progress') {
    const progress = normalizeBootstrapProgress(event, state, now)

    return progress ? { type: 'progress', ...progress } : null
  }

  if (event.type === 'complete') {
    return { type: 'complete', completedAt: safeTimestamp(now, Date.now()) }
  }

  if (event.type === 'failed') {
    const stage = safeBootstrapIdentifier(event.stage)

    return {
      type: 'failed',
      ...(stage && state.stages[stage] ? { stage } : {}),
      error: 'bootstrap_failed'
    }
  }

  if (event.type === 'unsupported-platform') {
    return { type: 'failed', error: 'bootstrap_failed' }
  }

  return null
}
