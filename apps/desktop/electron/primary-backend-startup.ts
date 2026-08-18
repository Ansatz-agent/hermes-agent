import type { FirstRunSetupDecision } from './first-run-setup-gate'
import { type ConnectionScope, requireAuthenticatedConnectionScope } from './auth-bridge'

export interface PrimaryBackendStartupOptions<Backend, RuntimeBackend, Remote, Connection> {
  connectionScope: ConnectionScope
  connectRemote: (remote: Remote, scope: ConnectionScope) => Promise<Connection>
  ensureLocalRuntime: (backend: Backend, scope: ConnectionScope) => Promise<RuntimeBackend>
  prepareLocalBackend: () => Backend | Promise<Backend>
  resolveRemote: () => Promise<Remote | null>
  waitForDecision: (backend: Backend) => Promise<FirstRunSetupDecision>
  waitForLocalStart: () => Promise<unknown>
}

export type PrimaryBackendStartupResult<RuntimeBackend, Connection> =
  { kind: 'local'; backend: RuntimeBackend } | { kind: 'remote'; connection: Connection }

export class FirstRunSetupResetError extends Error {
  readonly firstRunSetupReset = true

  constructor() {
    super('First-run setup was reset before a choice completed.')
    this.name = 'FirstRunSetupResetError'
  }
}

// Owns the production startHermes path up to the local process spawn. Keeping
// the full ordering here makes the first-run remote boundary executable in a
// test: an already-saved remote wins immediately; otherwise update exclusion
// and local backend resolution happen before the setup gate, and a remote Apply
// re-resolves persisted config without ever entering ensureRuntime/bootstrap.
export async function runPrimaryBackendStartup<Backend, RuntimeBackend, Remote, Connection>({
  connectionScope,
  connectRemote,
  ensureLocalRuntime,
  prepareLocalBackend,
  resolveRemote,
  waitForDecision,
  waitForLocalStart
}: PrimaryBackendStartupOptions<Backend, RuntimeBackend, Remote, Connection>): Promise<
  PrimaryBackendStartupResult<RuntimeBackend, Connection>
> {
  const authenticatedScope = requireAuthenticatedConnectionScope(connectionScope)
  const savedRemote = await resolveRemote()

  if (savedRemote) {
    return { kind: 'remote', connection: await connectRemote(savedRemote, authenticatedScope) }
  }

  await waitForLocalStart()

  const backend = await prepareLocalBackend()
  const decision = await waitForDecision(backend)

  if (decision === 'remote-applied') {
    const appliedRemote = await resolveRemote()

    if (!appliedRemote) {
      throw new Error('First-run remote setup completed without a saved remote backend.')
    }

    return { kind: 'remote', connection: await connectRemote(appliedRemote, authenticatedScope) }
  }

  if (decision === 'reset') {
    throw new FirstRunSetupResetError()
  }

  return { kind: 'local', backend: await ensureLocalRuntime(backend, authenticatedScope) }
}
