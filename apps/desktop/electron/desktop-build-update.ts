interface SourceUpdateStatus {
  supported: boolean
  currentSha?: string
  targetSha?: string
  updateAvailable?: boolean
  error?: string
}

interface InstalledBuildInput {
  managedPackagedApp: boolean
  installedSha?: string | null
  isAncestor: (older: string, newer: string) => Promise<boolean>
}

/** A fetched checkout is not proof that the installed app was rebuilt. */
export async function reconcileDesktopBuild<T extends SourceUpdateStatus>(
  status: T,
  { managedPackagedApp, installedSha, isAncestor }: InstalledBuildInput
) {
  const sha = /^[0-9a-f]{40}$/i

  if (!managedPackagedApp || !status.supported || status.error || status.updateAvailable ||
    !sha.test(installedSha || '') || !sha.test(status.currentSha || '') ||
    status.currentSha !== status.targetSha || installedSha === status.currentSha) {
    return status
  }

  // An unpublished/newer local app must never be offered a downgrade. Require
  // the Git graph to prove that its source precedes the checked-out target.
  if (!await isAncestor(installedSha!, status.currentSha!)) {
    return status
  }

  return {
    ...status,
    currentSha: installedSha!,
    behind: null,
    updateAvailable: true,
    reason: 'desktop-rebuild-required'
  }
}
