import path from 'node:path'

const COMMIT_RE = /^[0-9a-f]{40}$/i

export interface BundledRuntimeInput {
  packaged: boolean
  runtimeUsable: boolean
  installMethod?: string | null
  sourceCommit?: string | null
  payloadCommit?: string | null
  transactionPending?: boolean
}

export type BundledRuntimeDecision = 'not-applicable' | 'install' | 'reuse' | 'refresh' | 'payload-invalid'

export function resolveBundledBootstrapRoot({
  packaged,
  platform,
  resourcesPath
}: {
  packaged: boolean
  platform: NodeJS.Platform
  resourcesPath: string
}): string | null {
  const supportsBundledBootstrap = platform === 'darwin' || platform === 'win32'

  return packaged && supportsBundledBootstrap ? path.join(resourcesPath, 'bootstrap') : null
}

function isRealCommit(value: string | null | undefined): value is string {
  return typeof value === 'string' && COMMIT_RE.test(value) && !/^0+$/.test(value)
}

export function classifyBundledRuntime({
  packaged,
  runtimeUsable,
  installMethod,
  sourceCommit,
  payloadCommit,
  transactionPending = false
}: BundledRuntimeInput): BundledRuntimeDecision {
  if (!packaged) {
    return 'not-applicable'
  }

  if (!isRealCommit(payloadCommit)) {
    return 'payload-invalid'
  }

  if (!runtimeUsable) {
    return 'install'
  }

  if (transactionPending && isRealCommit(sourceCommit)) {
    return 'refresh'
  }

  if (installMethod !== 'desktop-bundle') {
    // A source marker without the final install-method stamp means a bundled
    // bootstrap was interrupted after source promotion. Resume it even if the
    // partially built venv already happens to import successfully.
    return isRealCommit(sourceCommit) ? 'refresh' : 'not-applicable'
  }

  if (sourceCommit === payloadCommit) {
    return 'reuse'
  }

  return 'refresh'
}
