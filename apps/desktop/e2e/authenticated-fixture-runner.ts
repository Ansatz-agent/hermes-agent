function aggregateMembers(error: unknown): unknown[] {
  return error instanceof AggregateError ? [...error.errors] : [error]
}

export async function runWithFixtureCleanup<T>(body: () => Promise<T>, cleanup: () => Promise<void>): Promise<T> {
  let primaryFailure: unknown
  let primaryFailed = false
  let result: T | undefined

  try {
    result = await body()
  } catch (error) {
    primaryFailed = true
    primaryFailure = error
  }

  let cleanupFailure: unknown
  let cleanupFailed = false
  try {
    await cleanup()
  } catch (error) {
    cleanupFailed = true
    cleanupFailure = error
  }

  if (primaryFailed) {
    if (cleanupFailed) {
      const combined = new AggregateError(
        [primaryFailure, ...aggregateMembers(cleanupFailure)],
        'Test body and fixture cleanup failed'
      )
      combined.cause = primaryFailure

      throw combined
    }

    throw primaryFailure
  }

  if (cleanupFailed) {
    throw new AggregateError(aggregateMembers(cleanupFailure), 'Fixture cleanup failed')
  }

  return result as T
}
