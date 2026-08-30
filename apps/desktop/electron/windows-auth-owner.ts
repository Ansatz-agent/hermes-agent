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
$expectedPaths = @(
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\python.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\pythonw.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\Scripts\python.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\Scripts\pythonw.exe'))
)
if ($env:HERMES_AUTH_OWNER_INCLUDE_LEGACY -eq '1') {
    $expectedPaths += @(
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\Scripts\python.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\Scripts\pythonw.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\python.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\pythonw.exe'))
    )
}
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$records = @()
Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
    Where-Object {
        if (-not $_.ExecutablePath) { return $false }
        $actual = [System.IO.Path]::GetFullPath([string]$_.ExecutablePath)
        return @($expectedPaths | Where-Object {
            [string]::Equals($_, $actual, [System.StringComparison]::OrdinalIgnoreCase)
        }).Count -gt 0
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
function Test-ExactOwnerCommand {
    param([object]$candidate)

    if (-not $candidate.ExecutablePath -or -not $candidate.CommandLine) { return $false }
    $executable = [System.IO.Path]::GetFullPath([string]$candidate.ExecutablePath)
    $quoted = '"' + $executable + '" -m hermes_cli.client_auth.runtime owner'
    $plain = $executable + ' -m hermes_cli.client_auth.runtime owner'

    return [string]::Equals($candidate.CommandLine, $quoted, [System.StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($candidate.CommandLine, $plain, [System.StringComparison]::OrdinalIgnoreCase)
}
function Get-OwnerSid {
    param([object]$candidate)

    $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid -ErrorAction Stop
    if ($owner.ReturnValue -ne 0 -or -not $owner.Sid) { return $null }

    return [string]$owner.Sid
}
function Wait-ProcessGone {
    param([int]$processId)

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $remaining = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if ($null -eq $remaining) { return $true }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)

    return $false
}
$excluded = @($env:HERMES_AUTH_OWNER_EXCLUDED_PIDS -split ',' | ForEach-Object {
    $value = 0
    if ([int]::TryParse($_, [ref]$value)) { $value }
})
if ($excluded -contains $targetPid) { exit 7 }
$process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction Stop
if ($null -eq $process) { Write-NoLongerOwner }
$expectedPaths = @(
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\python.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\pythonw.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\Scripts\python.exe')),
    [System.IO.Path]::GetFullPath((Join-Path $root 'auth-venv\Scripts\pythonw.exe'))
)
if ($env:HERMES_AUTH_OWNER_INCLUDE_LEGACY -eq '1') {
    $expectedPaths += @(
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\Scripts\python.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\Scripts\pythonw.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\python.exe')),
        [System.IO.Path]::GetFullPath((Join-Path $root 'venv\pythonw.exe'))
    )
}
$actualExecutable = if ($process.ExecutablePath) {
    [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
} else {
    $null
}
if (
    -not $actualExecutable -or
    @($expectedPaths | Where-Object {
        [string]::Equals($_, $actualExecutable, [System.StringComparison]::OrdinalIgnoreCase)
    }).Count -eq 0
) { Write-NoLongerOwner }
$rootExecutable = $actualExecutable
$owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
if ($owner.ReturnValue -ne 0 -or -not $owner.Sid) { exit 7 }
if ($owner.Sid -ne $expectedSid) { Write-NoLongerOwner }
if (-not (Test-ExactOwnerCommand $process)) { Write-NoLongerOwner }

# A venv\Scripts\pythonw.exe owner can be a redirector which launches a
# second pythonw.exe from uv's base installation.  That child is the process
# that commonly keeps the old source tree mapped.  Only consider direct
# children that still belong to this SID, have a Python executable, and carry
# the exact owner command line.  Unknown children are deliberately ignored;
# the root is never terminated on their behalf.
$childTargets = @()
$children = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | Where-Object {
    $_.ParentProcessId -eq $targetPid
})
if ($children.Count -gt 32) { exit 7 }
foreach ($child in $children) {
    if (-not $child.ExecutablePath) { continue }
    if ($excluded -contains [int]$child.ProcessId) { exit 7 }
    $basename = [System.IO.Path]::GetFileName([string]$child.ExecutablePath)
    if (
        -not [string]::Equals($basename, 'python.exe', [System.StringComparison]::OrdinalIgnoreCase) -and
        -not [string]::Equals($basename, 'pythonw.exe', [System.StringComparison]::OrdinalIgnoreCase)
    ) { continue }

    $childSid = Get-OwnerSid $child
    if ($null -eq $childSid) { exit 7 }
    if ($childSid -ne $expectedSid -or -not (Test-ExactOwnerCommand $child)) { continue }
    $childTargets += [pscustomobject]@{
        processId = [int]$child.ProcessId
        executablePath = [System.IO.Path]::GetFullPath([string]$child.ExecutablePath)
    }
}

$stoppedChildren = @()
foreach ($child in $childTargets) {
    # Revalidate the PID immediately before termination so a reused PID can
    # never be mistaken for the previously inventoried owner child.
    $currentChild = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $($child.ProcessId)" -ErrorAction SilentlyContinue
    if ($null -eq $currentChild) { continue }
    $currentChildExecutable = if ($currentChild.ExecutablePath) {
        [System.IO.Path]::GetFullPath([string]$currentChild.ExecutablePath)
    } else {
        $null
    }
    $childBasename = if ($currentChildExecutable) {
        [System.IO.Path]::GetFileName($currentChildExecutable)
    } else {
        $null
    }
    if (
        $currentChild.ParentProcessId -ne $targetPid -or
        -not $currentChildExecutable -or
        -not [string]::Equals($currentChildExecutable, $child.executablePath, [System.StringComparison]::OrdinalIgnoreCase) -or
        (
            -not [string]::Equals($childBasename, 'python.exe', [System.StringComparison]::OrdinalIgnoreCase) -and
            -not [string]::Equals($childBasename, 'pythonw.exe', [System.StringComparison]::OrdinalIgnoreCase)
        ) -or
        (Get-OwnerSid $currentChild) -ne $expectedSid -or
        -not (Test-ExactOwnerCommand $currentChild)
    ) { continue }

    $childTermination = Invoke-CimMethod -InputObject $currentChild -MethodName Terminate -ErrorAction Stop
    if ($childTermination.ReturnValue -ne 0) { exit 5 }
    if (-not (Wait-ProcessGone ([int]$child.ProcessId))) { exit 6 }
    $stoppedChildren += [int]$child.ProcessId
}

# Revalidate the root after child cleanup as well as before it.  This closes
# the PID-reuse window between the inventory and the final termination.
$process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $targetPid" -ErrorAction SilentlyContinue
if ($null -eq $process) { Write-NoLongerOwner }
$currentRootExecutable = if ($process.ExecutablePath) {
    [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
} else {
    $null
}
if (
    (Get-OwnerSid $process) -ne $expectedSid -or
    -not (Test-ExactOwnerCommand $process) -or
    -not $currentRootExecutable -or
    -not [string]::Equals($currentRootExecutable, $rootExecutable, [System.StringComparison]::OrdinalIgnoreCase) -or
    @($expectedPaths | Where-Object {
        [string]::Equals($_, $currentRootExecutable, [System.StringComparison]::OrdinalIgnoreCase)
    }).Count -eq 0
) { Write-NoLongerOwner }

$termination = Invoke-CimMethod -InputObject $process -MethodName Terminate -ErrorAction Stop
if ($termination.ReturnValue -ne 0) { exit 5 }
if (-not (Wait-ProcessGone $targetPid)) { exit 6 }
[pscustomobject]@{
    stopped = $true
    processId = $targetPid
    childProcessIds = @($stoppedChildren)
} | ConvertTo-Json -Compress
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
  includeLegacyVenv?: boolean
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

export function windowsAuthOwnerInterpreterPaths(activeRoot: string, includeLegacyVenv = false): string[] {
  const paths = [
    path.win32.join(activeRoot, 'auth-venv', 'python.exe'),
    path.win32.join(activeRoot, 'auth-venv', 'pythonw.exe'),
    path.win32.join(activeRoot, 'auth-venv', 'Scripts', 'python.exe'),
    path.win32.join(activeRoot, 'auth-venv', 'Scripts', 'pythonw.exe')
  ]

  if (includeLegacyVenv) {
    paths.push(
      path.win32.join(activeRoot, 'venv', 'Scripts', 'python.exe'),
      path.win32.join(activeRoot, 'venv', 'Scripts', 'pythonw.exe'),
      path.win32.join(activeRoot, 'venv', 'python.exe'),
      path.win32.join(activeRoot, 'venv', 'pythonw.exe')
    )
  }

  return paths
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

  const actualExecutable = canonicalWindowsPath(record.executablePath)
  const command = commandIdentity(record.commandLine)

  const expectedExecutables = windowsAuthOwnerInterpreterPaths(options.activeRoot, options.includeLegacyVenv).map(
    canonicalWindowsPath
  )

  return Boolean(
    actualExecutable &&
    expectedExecutables.includes(actualExecutable) &&
    command &&
    expectedExecutables.includes(canonicalWindowsPath(command.executable)) &&
    canonicalWindowsPath(command.executable) === actualExecutable &&
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
  includeLegacyVenv = false,
  runPowerShell = defaultRunPowerShell,
  now = () => performance.now()
}: {
  activeRoot: string
  callerPids?: number[]
  includeLegacyVenv?: boolean
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
    HERMES_AUTH_OWNER_EXCLUDED_PIDS: [...excludedPids].join(','),
    HERMES_AUTH_OWNER_INCLUDE_LEGACY: includeLegacyVenv ? '1' : '0'
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
      excludedPids,
      includeLegacyVenv
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
