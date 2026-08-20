import type {
  DesktopBootstrapProgress,
  DesktopBootstrapProgressUnit,
  DesktopBootstrapStageDescriptor,
  DesktopSafeBootstrapStageResult,
  DesktopSafeBootstrapState
} from '@/global'

export interface AuthBootstrapStageView {
  descriptor: DesktopBootstrapStageDescriptor
  elapsedMs: number | null
  result: DesktopSafeBootstrapStageResult
}

export interface AuthBootstrapProgressView {
  completedStages: number
  current: AuthBootstrapStageView | null
  failed: AuthBootstrapStageView | null
  overallFraction: number
  stages: AuthBootstrapStageView[]
  totalStages: number
}

const EMPTY_RESULT: DesktopSafeBootstrapStageResult = {
  state: 'pending',
  durationMs: null,
  startedAt: null,
  error: null,
  progress: null
}

export function deriveAuthBootstrapProgress(
  state: DesktopSafeBootstrapState,
  now = Date.now()
): AuthBootstrapProgressView {
  const descriptors = state.manifest?.stages || []

  const stages = descriptors.map(descriptor => {
    const result = state.stages[descriptor.name] || EMPTY_RESULT

    const elapsedMs =
      result.state === 'running' && typeof result.startedAt === 'number'
        ? Math.max(0, now - result.startedAt)
        : null

    return { descriptor, elapsedMs, result }
  })

  const completedStages = stages.filter(
    stage => stage.result.state === 'succeeded' || stage.result.state === 'skipped'
  ).length

  const totalStages = stages.length
  const current = stages.find(stage => stage.result.state === 'running') || null

  const failed = state.failedStage
    ? stages.find(stage => stage.descriptor.name === state.failedStage && stage.result.state === 'failed') || null
    : stages.find(stage => stage.result.state === 'failed') || null

  return {
    completedStages,
    current,
    failed,
    overallFraction: totalStages > 0 ? completedStages / totalStages : 0,
    stages,
    totalStages
  }
}

export function formatBootstrapElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))

  if (seconds < 60) {
    return `${seconds}s`
  }

  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
  }

  return `${minutes}:${String(remainder).padStart(2, '0')}`
}

function decimal(value: number, digits = 1): string {
  return value.toFixed(digits).replace(/\.0$/, '')
}

export function formatBootstrapAmount(value: number, unit: DesktopBootstrapProgressUnit): string {
  if (unit !== 'bytes') {
    return String(value)
  }

  if (value >= 1_000_000_000) {
    return `${decimal(value / 1_000_000_000)} GB`
  }

  if (value >= 1_000_000) {
    return `${decimal(value / 1_000_000)} MB`
  }

  if (value >= 1_000) {
    return `${decimal(value / 1_000)} KB`
  }

  return `${value} B`
}

export function progressFraction(progress: Pick<DesktopBootstrapProgress, 'completed' | 'total'>): number | null {
  if (typeof progress.total !== 'number' || !Number.isFinite(progress.total) || progress.total <= 0) {
    return null
  }

  return Math.min(1, Math.max(0, progress.completed / progress.total))
}
