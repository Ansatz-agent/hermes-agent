import type { DesktopRuntimeGate } from './desktop-runtime-gate'

type RuntimeScope = { scope: 'runtime' }

type InstallFirstRuntimeOptions<Candidate> = {
  resolveBackend: () => Candidate | null
  ensureRuntime: (candidate: Candidate, options: RuntimeScope) => Promise<unknown>
  runtimeGate: Pick<DesktopRuntimeGate, 'prepare'>
  startAuthBridge: () => Promise<unknown>
}

async function prepareInstallFirstRuntime<Candidate>({
  resolveBackend,
  ensureRuntime,
  runtimeGate,
  startAuthBridge
}: InstallFirstRuntimeOptions<Candidate>): Promise<void> {
  await runtimeGate.prepare(async () => {
    const candidate = resolveBackend()

    if (candidate !== null) {
      await ensureRuntime(candidate, { scope: 'runtime' })
    }
  })

  await startAuthBridge()
}

export { prepareInstallFirstRuntime }
export type { InstallFirstRuntimeOptions }
