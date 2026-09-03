import fs from 'node:fs/promises'
import path from 'node:path'

const LEGACY_ANSATZ_PARTITIONS = Object.freeze(['hermes-embed', 'hermes-preview', 'hermes-remote-oauth'] as const)

type LegacyCleanupOptions = {
  onError?: (partitionName: string) => void
}

async function lstatOrNull(target: string) {
  try {
    return await fs.lstat(target)
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') {
      return null
    }

    throw error
  }
}

export async function removeLegacyAnsatzPartitions(
  userDataRoot: string,
  options: LegacyCleanupOptions = {}
): Promise<string[]> {
  const resolvedUserData = path.resolve(userDataRoot)
  const partitionsRoot = path.resolve(resolvedUserData, 'Partitions')

  if (path.dirname(partitionsRoot) !== resolvedUserData) {
    return []
  }

  let rootDetails

  try {
    rootDetails = await lstatOrNull(partitionsRoot)
  } catch {
    options.onError?.('Partitions')

    return []
  }

  if (!rootDetails || !rootDetails.isDirectory() || rootDetails.isSymbolicLink()) {
    return []
  }

  const removed: string[] = []

  for (const partitionName of LEGACY_ANSATZ_PARTITIONS) {
    const target = path.resolve(partitionsRoot, partitionName)

    if (path.dirname(target) !== partitionsRoot) {
      continue
    }

    try {
      const details = await lstatOrNull(target)

      if (!details || !details.isDirectory() || details.isSymbolicLink()) {
        continue
      }

      await fs.rm(target, { force: false, recursive: true })
      removed.push(partitionName)
    } catch {
      options.onError?.(partitionName)
    }
  }

  return removed
}

export { LEGACY_ANSATZ_PARTITIONS }
