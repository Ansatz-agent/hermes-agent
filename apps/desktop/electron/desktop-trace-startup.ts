export async function prepareLocalTraceCapture<T>({
  startListener,
  onDiagnostic,
  scheduleRetry
}: {
  startListener: () => Promise<T>
  onDiagnostic: (message: string) => void
  scheduleRetry: () => void
}): Promise<T | null> {
  try {
    return await startListener()
  } catch {
    onDiagnostic('trace capture unavailable')
    scheduleRetry()

    return null
  }
}
