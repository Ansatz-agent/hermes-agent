import { execFileSync } from 'node:child_process'
import { realpathSync } from 'node:fs'

export interface WindowsProcessRow {
  pid: number
  parentPid: number
  commandLine: string
  name: string
}

function normalizeWindowsCommandPath(candidate: string): string {
  let normalized = candidate.trim()
  if (/^\\\\\?\\UNC\\/i.test(normalized)) {
    normalized = `\\\\${normalized.slice(8)}`
  } else {
    normalized = normalized.replace(/^\\\\\?\\/i, '')
  }

  return normalized.replace(/[\\/]+$/, '').toLowerCase()
}

export function windowsPathAliases(
  candidate: string,
  resolveFinalPath: (value: string) => string = value => realpathSync.native(value)
): string[] {
  const aliases = new Set<string>()
  aliases.add(normalizeWindowsCommandPath(candidate))
  try {
    aliases.add(normalizeWindowsCommandPath(resolveFinalPath(candidate)))
  } catch {
    // The original spelling still provides a useful command-line check.
  }

  return [...aliases].filter(Boolean)
}

export function descendantsOf(rows: WindowsProcessRow[], rootPid: number): WindowsProcessRow[] {
  const childrenByParent = new Map<number, WindowsProcessRow[]>()
  for (const row of rows) {
    const children = childrenByParent.get(row.parentPid) ?? []
    children.push(row)
    childrenByParent.set(row.parentPid, children)
  }
  for (const children of childrenByParent.values()) {
    children.sort((left, right) => left.pid - right.pid)
  }

  const descendants: WindowsProcessRow[] = []
  const queue = [...(childrenByParent.get(rootPid) ?? [])]
  const seen = new Set<number>([rootPid])
  while (queue.length > 0) {
    const row = queue.shift()!
    if (seen.has(row.pid)) {
      continue
    }
    seen.add(row.pid)
    descendants.push(row)
    queue.push(...(childrenByParent.get(row.pid) ?? []))
  }

  return descendants
}

function sameWindowsProcess(left: WindowsProcessRow, right: WindowsProcessRow): boolean {
  return (
    left.pid === right.pid &&
    left.parentPid === right.parentPid &&
    left.name.toLowerCase() === right.name.toLowerCase() &&
    left.commandLine === right.commandLine
  )
}

export function capturedProcessIdsDeepestFirst(
  capturedRows: readonly WindowsProcessRow[],
  currentRows: readonly WindowsProcessRow[]
): number[] {
  const capturedByPid = new Map(capturedRows.map(row => [row.pid, row]))
  const currentByPid = new Map(currentRows.map(row => [row.pid, row]))
  const depthByPid = new Map<number, number>()

  const depthOf = (pid: number, visiting = new Set<number>()): number => {
    const known = depthByPid.get(pid)
    if (known !== undefined) {
      return known
    }
    if (visiting.has(pid)) {
      return 0
    }

    const row = capturedByPid.get(pid)
    if (!row || !capturedByPid.has(row.parentPid)) {
      depthByPid.set(pid, 0)
      return 0
    }

    const nextVisiting = new Set(visiting)
    nextVisiting.add(pid)
    const depth = depthOf(row.parentPid, nextVisiting) + 1
    depthByPid.set(pid, depth)
    return depth
  }

  return capturedRows
    .filter(row => {
      const current = currentByPid.get(row.pid)
      return current !== undefined && sameWindowsProcess(row, current)
    })
    .sort((left, right) => depthOf(right.pid) - depthOf(left.pid) || right.pid - left.pid)
    .map(row => row.pid)
}

export function windowsProcessSnapshot(): WindowsProcessRow[] {
  if (process.platform !== 'win32') {
    throw new Error('Windows process snapshot requires win32')
  }

  const script = `Get-CimInstance Win32_Process |
    Select-Object ProcessId,ParentProcessId,Name,CommandLine |
    ConvertTo-Json -Compress`
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8'
  }).trim()
  if (!output) {
    throw new Error('Windows process snapshot returned no JSON')
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(output)
  } catch (error) {
    throw new Error(`Windows process snapshot returned invalid JSON: ${(error as Error).message}`)
  }
  const values = Array.isArray(parsed) ? parsed : [parsed]

  return values.map((value, index) => {
    if (!value || typeof value !== 'object') {
      throw new Error(`Windows process snapshot row ${index} is not an object`)
    }
    const row = value as Record<string, unknown>
    if (
      !Number.isInteger(row.ProcessId) ||
      !Number.isInteger(row.ParentProcessId) ||
      typeof row.Name !== 'string' ||
      (row.CommandLine !== null && typeof row.CommandLine !== 'string')
    ) {
      throw new Error(`Windows process snapshot row ${index} is invalid`)
    }

    return {
      pid: row.ProcessId as number,
      parentPid: row.ParentProcessId as number,
      name: row.Name,
      commandLine: row.CommandLine ?? ''
    } as WindowsProcessRow
  })
}
