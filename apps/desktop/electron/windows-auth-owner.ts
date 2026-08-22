import { execFile } from 'node:child_process'
import path from 'node:path'

const OWNER_ARGUMENTS = '-m hermes_cli.client_auth.runtime owner'
const POWERSHELL_TIMEOUT_MS = 10_000
const POWERSHELL_TOTAL_TIMEOUT_MS = 30_000
const MAX_CANDIDATE_PROCESSES = 64

const INVENTORY_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = $env:HERMES_AUTH_OWNER_ROOT
if (-not $root) { exit 2 }
$expected = [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\python.exe'))
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$records = @()
Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
    Where-Object {
        $_.ExecutablePath -and
        [string]::Equals(
            [System.IO.Path]::GetFullPath($_.ExecutablePath),
            $expected,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    } |
    ForEach-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction Stop
        $records += [pscustomobject]@{
            processId = [int]$_.ProcessId
            executablePath = [string]$_.ExecutablePath
            commandLine = if ($null -eq $_.CommandLine) { $null } else { [string]$_.CommandLine }
            ownerSid = if ($owner.ReturnValue -eq 0) { [string]$owner.Sid } else { $null }
        }
    }
[pscustomobject]@{ currentSid = $currentSid; processes = @($records) } |
    ConvertTo-Json -Compress -Depth 4
`

const TERMINATE_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = $env:HERMES_AUTH_OWNER_ROOT
$expectedSid = $env:HERMES_AUTH_OWNER_SID
$targetPid = 0
if (
    -not $root -or
    -not $expectedSid -or
    -not [int]::TryParse($env:HERMES_AUTH_OWNER_PID, [ref]$targetPid) -or
    $targetPid -le 0
) { exit 2 }
function Write-NoLongerOwner {
    [pscustomobject]@{ stopped = $false; noLongerOwner = $true; processId = $targetPid } |
        ConvertTo-Json -Compress
    exit 0
}
$excluded = @($env:HERMES_AUTH_OWNER_EXCLUDED_PIDS -split ',' | ForEach-Object {
    $value = 0
    if ([int]::TryParse($_, [ref]$value)) { $value }
})
if ($excluded -contains $targetPid) { exit 7 }
$process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction Stop
if ($null -eq $process) { Write-NoLongerOwner }
$expected = [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\python.exe'))
if (
    -not $process.ExecutablePath -or
    -not [string]::Equals(
        [System.IO.Path]::GetFullPath($process.ExecutablePath),
        $expected,
        [System.StringComparison]::OrdinalIgnoreCase
    )
) { Write-NoLongerOwner }
$owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
if ($owner.ReturnValue -ne 0 -or -not $owner.Sid) { exit 7 }
if ($owner.Sid -ne $expectedSid) { Write-NoLongerOwner }
$expectedQuoted = '"' + $expected + '" -m hermes_cli.client_auth.runtime owner'
$expectedPlain = $expected + ' -m hermes_cli.client_auth.runtime owner'
if (
    -not [string]::Equals($process.CommandLine, $expectedQuoted, [System.StringComparison]::OrdinalIgnoreCase) -and
    -not [string]::Equals($process.CommandLine, $expectedPlain, [System.StringComparison]::OrdinalIgnoreCase)
) { Write-NoLongerOwner }
$termination = Invoke-CimMethod -InputObject $process -MethodName Terminate -ErrorAction Stop
if ($termination.ReturnValue -ne 0) { exit 5 }
$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    $remaining = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction SilentlyContinue
    if ($null -eq $remaining) { break }
    Start-Sleep -Milliseconds 50
} while ([DateTime]::UtcNow -lt $deadline)
if ($null -ne $remaining) { exit 6 }
[pscustomobject]@{ stopped = $true; processId = $targetPid } | ConvertTo-Json -Compress
`

export type WindowsProcessRecord = {
  processId: number
  executablePath: string | null
  commandLine: string | null
  ownerSid: string | null
}

type IdentityOptions = {
  activeRoot: string
  currentSid: string
  excludedPids: ReadonlySet<number>
}

type PowerShellResult = {
  status: number | null
  stdout: string
  stderr: string
  error?: Error
}

type PowerShellRunner = (
  command: string,
  args: string[],
  options: {
    env: NodeJS.ProcessEnv
    encoding: 'utf8'
    timeout: number
    windowsHide: boolean
    maxBuffer: number
  }
) => Promise<PowerShellResult>

function canonicalWindowsPath(value: string): string | null {
  if (!value || !path.win32.isAbsolute(value)) {
    return null
  }

  try {
    return path.win32
      .normalize(value)
      .replace(/[\\/]+$/, '')
      .toLocaleLowerCase('en-US')
  } catch {
    return null
  }
}

function commandIdentity(commandLine: string): { executable: string; arguments: string } | null {
  const trimmed = commandLine.trim()

  if (!trimmed) {
    return null
  }

  if (trimmed.startsWith('"')) {
    const closingQuote = trimmed.indexOf('"', 1)

    if (closingQuote <= 1) {
      return null
    }

    const executable = trimmed.slice(1, closingQuote)
    const argumentsValue = trimmed.slice(closingQuote + 1).trim()

    if (argumentsValue.includes('"')) {
      return null
    }

    return { executable, arguments: argumentsValue }
  }

  const separator = trimmed.search(/\s/)

  if (separator <= 0) {
    return null
  }

  return {
    executable: trimmed.slice(0, separator),
    arguments: trimmed.slice(separator).trim()
  }
}

export function isExactWindowsAuthOwnerProcess(record: WindowsProcessRecord, options: IdentityOptions): boolean {
  if (
    !Number.isSafeInteger(record.processId) ||
    record.processId <= 0 ||
    options.excludedPids.has(record.processId) ||
    !record.executablePath ||
    !record.commandLine ||
    !record.ownerSid ||
    record.ownerSid !== options.currentSid
  ) {
    return false
  }

  const expectedExecutable = canonicalWindowsPath(
    path.win32.join(options.activeRoot, 'auth-venv', 'python.exe')
  )

  const actualExecutable = canonicalWindowsPath(record.executablePath)
  const command = commandIdentity(record.commandLine)

  return Boolean(
    expectedExecutable &&
    actualExecutable === expectedExecutable &&
    command &&
    canonicalWindowsPath(command.executable) === expectedExecutable &&
    command.arguments === OWNER_ARGUMENTS
  )
}

function encodePowerShell(script: string): string {
  return Buffer.from(script, 'utf16le').toString('base64')
}

function defaultRunPowerShell(
  command: string,
  args: string[],
  options: Parameters<PowerShellRunner>[2]
): Promise<PowerShellResult> {
  return new Promise(resolve => {
    execFile(command, args, options, (error, stdout, stderr) => {
      const code = error && 'code' in error ? error.code : 0

      resolve({
        status: typeof code === 'number' ? code : error ? null : 0,
        stdout: stdout || '',
        stderr: stderr || '',
        error: error || undefined
      })
    })
  })
}

function parseInventory(stdout: string): { currentSid: string; processes: WindowsProcessRecord[] } {
  let parsed: unknown

  try {
    parsed = JSON.parse(stdout.replace(/^\uFEFF/, '').trim())
  } catch {
    throw new Error('Windows auth owner inspection returned invalid data')
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Windows auth owner inspection returned invalid data')
  }

  const value = parsed as Record<string, unknown>

  if (
    typeof value.currentSid !== 'string' ||
    !value.currentSid.startsWith('S-1-') ||
    !Array.isArray(value.processes) ||
    value.processes.length > MAX_CANDIDATE_PROCESSES
  ) {
    throw new Error('Windows auth owner inspection returned invalid data')
  }

  return {
    currentSid: value.currentSid,
    processes: value.processes.map(item => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) {
        throw new Error('Windows auth owner inspection returned invalid data')
      }

      const record = item as Record<string, unknown>

      if (
        !Number.isSafeInteger(record.processId) ||
        (record.processId as number) <= 0 ||
        typeof record.executablePath !== 'string' ||
        !record.executablePath ||
        typeof record.commandLine !== 'string' ||
        !record.commandLine ||
        typeof record.ownerSid !== 'string' ||
        !record.ownerSid.startsWith('S-1-')
      ) {
        throw new Error('Windows auth owner inspection returned invalid data')
      }

      return {
        processId: record.processId as number,
        executablePath: record.executablePath,
        commandLine: record.commandLine,
        ownerSid: record.ownerSid
      }
    })
  }
}

async function checkedPowerShell(
  runPowerShell: PowerShellRunner,
  script: string,
  env: NodeJS.ProcessEnv,
  now: () => number,
  deadlineAt: number
): Promise<PowerShellResult> {
  const remainingMs = deadlineAt - now()

  if (remainingMs <= 0) {
    throw new Error('Windows auth owner retirement exceeded its aggregate deadline')
  }

  return await runPowerShell(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-EncodedCommand', encodePowerShell(script)],
    {
      env,
      encoding: 'utf8',
      timeout: Math.max(1, Math.min(POWERSHELL_TIMEOUT_MS, Math.ceil(remainingMs))),
      windowsHide: true,
      maxBuffer: 1024 * 1024
    }
  )
}

export async function retireExactWindowsAuthOwners({
  activeRoot,
  callerPids = [process.pid, process.ppid],
  runPowerShell = defaultRunPowerShell,
  now = () => performance.now()
}: {
  activeRoot: string
  callerPids?: number[]
  runPowerShell?: PowerShellRunner
  now?: () => number
}): Promise<{ inspected: number; stopped: number }> {
  const deadlineAt = now() + POWERSHELL_TOTAL_TIMEOUT_MS
  const excludedPids = new Set(callerPids.filter(pid => Number.isSafeInteger(pid) && pid > 0))

  const baseEnvironment: NodeJS.ProcessEnv = {
    SystemRoot: process.env.SystemRoot,
    WINDIR: process.env.WINDIR,
    PATH: process.env.PATH,
    HERMES_AUTH_OWNER_ROOT: activeRoot,
    HERMES_AUTH_OWNER_EXCLUDED_PIDS: [...excludedPids].join(',')
  }

  const inventoryResult = await checkedPowerShell(runPowerShell, INVENTORY_SCRIPT, baseEnvironment, now, deadlineAt)

  if (inventoryResult.error || inventoryResult.status !== 0) {
    throw new Error('Windows auth owner inspection failed')
  }

  const inventory = parseInventory(inventoryResult.stdout)

  const selected = inventory.processes.filter(record =>
    isExactWindowsAuthOwnerProcess(record, {
      activeRoot,
      currentSid: inventory.currentSid,
      excludedPids
    })
  )

  let stopped = 0

  for (const record of selected) {
    const terminationResult = await checkedPowerShell(
      runPowerShell,
      TERMINATE_SCRIPT,
      {
        ...baseEnvironment,
        HERMES_AUTH_OWNER_PID: String(record.processId),
        HERMES_AUTH_OWNER_SID: inventory.currentSid
      },
      now,
      deadlineAt
    )

    let proof: unknown = null

    try {
      proof = JSON.parse(terminationResult.stdout.replace(/^\uFEFF/, '').trim())
    } catch {
      proof = null
    }

    const proofRecord = proof && typeof proof === 'object' ? (proof as Record<string, unknown>) : null
    const stoppedNow = proofRecord?.stopped === true && proofRecord.noLongerOwner === undefined
    const noLongerOwner = proofRecord?.stopped === false && proofRecord.noLongerOwner === true

    if (
      terminationResult.error ||
      terminationResult.status !== 0 ||
      !proofRecord ||
      proofRecord.processId !== record.processId ||
      (!stoppedNow && !noLongerOwner)
    ) {
      throw new Error('Windows auth owner could not be safely retired')
    }

    if (stoppedNow) {
      stopped += 1
    }
  }

  return { inspected: inventory.processes.length, stopped }
}
