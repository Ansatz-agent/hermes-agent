import { useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ErrorIcon } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { Progress } from '@/components/ui/progress'
import type { DesktopBootstrapProgress, DesktopBootstrapStageState, DesktopSafeBootstrapState } from '@/global'
import { useI18n } from '@/i18n'
import {
  type AuthBootstrapStageView,
  deriveAuthBootstrapProgress,
  formatBootstrapAmount,
  formatBootstrapElapsed,
  progressFraction,
  sanitizeAuthBootstrapText
} from '@/lib/auth-bootstrap-progress'

interface AuthBootstrapProgressProps {
  mode: 'auth' | 'runtime'
  now: number
  onLogout?: () => void
  onRetry?: () => void
  state: DesktopSafeBootstrapState
}

function StageIcon({ state }: { state: DesktopBootstrapStageState }) {
  if (state === 'running') {
    return <Loader className="size-5" type="fourier-flow" />
  }

  if (state === 'succeeded' || state === 'skipped') {
    return <Codicon className="text-(--ui-text-secondary)" name="check" size="0.8125rem" />
  }

  if (state === 'failed') {
    return <ErrorIcon size="0.875rem" />
  }

  return <span aria-hidden="true" className="size-1.5 rounded-full border border-(--ui-stroke-secondary)" />
}

function ProgressDetail({ progress }: { progress: DesktopBootstrapProgress }) {
  const { t } = useI18n()
  const copy = t.auth.bootstrap
  const fraction = progressFraction(progress)
  const completed = formatBootstrapAmount(progress.completed, progress.unit)
  const unit = progress.unit === 'bytes' ? '' : copy.units[progress.unit]

  const detail =
    fraction === null
      ? copy.unknownProgress(completed, unit)
      : copy.knownProgress(
          completed,
          formatBootstrapAmount(progress.total!, progress.unit),
          unit,
          Math.round(fraction * 100)
        )

  const label = sanitizeAuthBootstrapText(progress.label, progress.stage)

  return (
    <div className="mt-2 space-y-1.5">
      <Progress
        animated={fraction === null}
        aria-label={label}
        indeterminate={fraction === null}
        value={fraction ?? 0}
      />
      <p className="text-xs tabular-nums text-(--ui-text-secondary)">{detail}</p>
    </div>
  )
}

function StageRow({ stage }: { stage: AuthBootstrapStageView }) {
  const { t } = useI18n()
  const copy = t.auth.bootstrap
  const { result } = stage
  const stateCopy = copy.stageStates[result.state]

  const status =
    result.state === 'running' && stage.elapsedMs !== null
      ? copy.runningWithElapsed(stateCopy, formatBootstrapElapsed(stage.elapsedMs))
      : stateCopy

  return (
    <li className="py-1.5">
      <div className="flex items-center gap-2">
        <span className="flex size-5 shrink-0 items-center justify-center">
          <StageIcon state={result.state} />
        </span>
        <span className="min-w-0 flex-1 truncate text-sm">{stage.descriptor.title || stage.descriptor.name}</span>
        <span className="shrink-0 text-xs tabular-nums text-(--ui-text-secondary)">{status}</span>
      </div>
      {result.state === 'running' && result.progress ? (
        <div className="ml-7">
          <ProgressDetail progress={result.progress} />
        </div>
      ) : null}
    </li>
  )
}

export function AuthBootstrapProgress({ mode, now, onLogout, onRetry, state }: AuthBootstrapProgressProps) {
  const { t } = useI18n()
  const copy = t.auth.bootstrap
  const view = useMemo(() => deriveAuthBootstrapProgress(state, now), [now, state])

  const currentIndex = view.current
    ? view.stages.findIndex(stage => stage.descriptor.name === view.current?.descriptor.name) + 1
    : 0

  if (mode === 'auth') {
    return (
      <div className="mt-6 space-y-3" role="status">
        <p className="font-medium">{copy.preparingAuthTitle}</p>
        {view.current ? (
          <>
            <p className="text-sm text-(--ui-text-secondary)">
              {copy.stagePosition(
                currentIndex,
                view.totalStages,
                view.current.descriptor.title || view.current.descriptor.name
              )}
            </p>
            {view.current.result.progress ? <ProgressDetail progress={view.current.result.progress} /> : null}
          </>
        ) : null}
        {state.error ? (
          <div className="space-y-2" role="alert">
            <p className="text-sm text-destructive">{copy.authPreparationFailed}</p>
            {view.failed ? (
              <p className="text-sm text-(--ui-text-secondary)">
                {copy.failedAt(view.failed.descriptor.title || view.failed.descriptor.name)}
              </p>
            ) : null}
            {onRetry ? (
              <Button onClick={onRetry} size="inline" variant="textStrong">
                {t.auth.retry}
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    )
  }

  return (
    <div className="mt-6 space-y-4" role="status">
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-3 text-sm">
          <span>{copy.stagesComplete(view.completedStages, view.totalStages)}</span>
          {view.current?.elapsedMs !== null && view.current ? (
            <span className="tabular-nums text-(--ui-text-secondary)">
              {copy.elapsed(formatBootstrapElapsed(view.current.elapsedMs))}
            </span>
          ) : null}
        </div>
        <Progress aria-label={copy.overallProgressLabel} value={view.overallFraction} />
      </div>

      <ol className="divide-y divide-(--ui-stroke-tertiary)">
        {view.stages.map(stage => (
          <StageRow key={stage.descriptor.name} stage={stage} />
        ))}
      </ol>

      {state.error ? (
        <div className="space-y-2" role="alert">
          <p className="text-sm text-destructive">{copy.runtimePreparationFailed}</p>
          {view.failed ? (
            <p className="text-sm text-(--ui-text-secondary)">
              {copy.failedAt(view.failed.descriptor.title || view.failed.descriptor.name)}
            </p>
          ) : null}
        </div>
      ) : null}

      <div className="flex items-center gap-4">
        {state.error && onRetry ? (
          <Button onClick={onRetry} size="inline" variant="textStrong">
            {t.auth.retry}
          </Button>
        ) : null}
        {onLogout ? (
          <Button onClick={onLogout} size="inline" variant="text">
            {t.auth.signOut}
          </Button>
        ) : null}
      </div>
    </div>
  )
}
