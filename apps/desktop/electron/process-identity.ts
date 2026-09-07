/**
 * Windows process-identity helpers shared by the desktop backend lifecycle.
 *
 * A child can exit between spawn() returning its PID and the identity probe
 * that records its creation time. Keep that race explicit instead of exposing
 * a misleading "ownership persistence" error to the renderer.
 */

export const WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE = 3

export function windowsProcessStartMarkerCommand(pid: number): string {
  if (!Number.isInteger(pid) || pid <= 0) {
    throw new TypeError(`Invalid Windows process id: ${pid}`)
  }

  // Do not use Get-Process -ErrorAction Stop here. A process that exits in the
  // probe window is an expected lifecycle race, not a PowerShell exception.
  // Exit with a private code so the caller can classify it as ESRCH without
  // parsing localized PowerShell stderr.
  return [
    `$p = Get-Process -Id ${pid} -ErrorAction SilentlyContinue`,
    `if ($null -eq $p) { exit ${WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE} }`,
    'try { $p.StartTime.ToUniversalTime().Ticks } catch { exit 3 }'
  ].join('; ')
}

export function isProcessGoneError(error: unknown): boolean {
  const candidate = error as { code?: unknown; message?: unknown; stderr?: unknown }
  const code = candidate?.code

  if (code === 'ENOENT' || code === 'ESRCH' || code === WINDOWS_PROCESS_NOT_FOUND_EXIT_CODE) {
    return true
  }

  const detail = `${String(candidate?.message ?? '')}\n${String(candidate?.stderr ?? '')}`

  return /(?:no\s+process|process\s+not\s+found|cannot\s+find\s+(?:the\s+)?process|noprocessfoundforgivenid|process\s+has\s+exited|process\s+no\s+longer\s+exists)/i.test(
    detail
  )
}

export function backendExitedBeforeOwnershipError(
  pid: number,
  cause?: unknown
): Error & { code: string; cause?: unknown } {
  const error = new Error(`Hermes backend process ${pid} exited before ownership could be recorded.`) as Error & {
    code: string
    cause?: unknown
  }

  error.code = 'BACKEND_EXITED'

  if (cause !== undefined) {
    error.cause = cause
  }

  return error
}
