[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallerPath,
    [Parameter(Mandatory = $true)][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [switch]$RunAuthE2E,
    [switch]$RunCredentialE2E,
    [string]$AuthLogPath = '',
    [int]$StartupTimeoutSeconds = 60,
    [int]$StabilitySeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-SmokeLog([string]$Message) {
    $line = "[{0:o}] {1}" -f [DateTime]::UtcNow, $Message
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Wait-ForCondition([scriptblock]$Condition, [int]$TimeoutSeconds, [string]$FailureMessage) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 500
    }
    throw $FailureMessage
}

$InstallerPath = [IO.Path]::GetFullPath($InstallerPath)
$LogPath = [IO.Path]::GetFullPath($LogPath)
if ($RunAuthE2E -or $RunCredentialE2E) {
    if ([String]::IsNullOrWhiteSpace($AuthLogPath)) {
        throw 'AuthLogPath is required for an authentication E2E mode'
    }
    $AuthLogPath = [IO.Path]::GetFullPath($AuthLogPath)
}

function Get-AvailableLoopbackPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "Installer not found: $InstallerPath"
}
if (-not $InstallerPath.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer must be an NSIS .exe: $InstallerPath"
}
if ([String]::IsNullOrWhiteSpace($ExpectedCommit)) {
    throw 'ExpectedCommit must not be empty'
}
if ([String]::IsNullOrWhiteSpace($ExpectedVersion)) {
    throw 'ExpectedVersion must not be empty'
}
if ($StartupTimeoutSeconds -le 0 -or $StabilitySeconds -lt 0) {
    throw 'Timeout values must be non-negative and startup timeout must be greater than zero'
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
Set-Content -LiteralPath $LogPath -Value '' -Encoding UTF8
if ($RunAuthE2E -and $RunCredentialE2E) {
    throw 'RunAuthE2E and RunCredentialE2E are mutually exclusive'
}
if ($RunAuthE2E -or $RunCredentialE2E) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $AuthLogPath) | Out-Null
    Set-Content -LiteralPath $AuthLogPath -Value '' -Encoding UTF8
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("hermes-nsis-smoke-" + [Guid]::NewGuid().ToString('N'))
$installDir = Join-Path $testRoot 'install'
$userDataDir = Join-Path $testRoot 'electron-user-data'
$hermesHome = Join-Path $testRoot 'hermes-home'
$workspace = Join-Path $testRoot 'workspace'
$exePath = Join-Path $installDir 'Hermes.exe'
$uninstallerPath = Join-Path $installDir 'Uninstall Hermes.exe'
$savedEnvironment = @{}
$startedProcess = $null
$installed = $false
$primaryFailure = $null
$cleanupFailures = [Collections.Generic.List[string]]::new()
$credentialUsername = ''
$credentialPassword = ''

if ($RunCredentialE2E) {
    $credentialUsername = [Environment]::GetEnvironmentVariable('HERMES_E2E_USERNAME')
    $credentialPassword = [Environment]::GetEnvironmentVariable('HERMES_E2E_PASSWORD')
    Remove-Item -LiteralPath "Env:HERMES_E2E_USERNAME" -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "Env:HERMES_E2E_PASSWORD" -ErrorAction SilentlyContinue
    if ([String]::IsNullOrWhiteSpace($credentialUsername) -and
        [String]::IsNullOrWhiteSpace($credentialPassword)) {
        throw 'GitHub Actions test-account Secrets are not configured'
    }
    if ([String]::IsNullOrWhiteSpace($credentialUsername) -or
        [String]::IsNullOrWhiteSpace($credentialPassword)) {
        throw 'GitHub Actions test-account Secret configuration is incomplete'
    }
}

$isolatedEnvironment = @{
    HERMES_DESKTOP_CWD = $workspace
    HERMES_DESKTOP_HERMES = $null
    HERMES_DESKTOP_HERMES_ROOT = $null
    HERMES_DESKTOP_IGNORE_EXISTING = '1'
    HERMES_DESKTOP_TEST_MODE = 'fresh-install'
    HERMES_DESKTOP_USER_DATA_DIR = $userDataDir
    HERMES_HOME = $hermesHome
    HERMES_DESKTOP_SKIP_QUIT_CONFIRM = '1'
    HERMES_DESKTOP_APP_NAME = "HermesWindowsAcceptance-$([Guid]::NewGuid().ToString('N'))"
}
if ($RunAuthE2E) {
    $isolatedEnvironment['HERMES_E2E_INSTALLED_BINARY'] = $exePath
}
$credentialEnvironmentNames = @(
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_TOKEN',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'CUSTOM_API_KEY',
    'GEMINI_BASE_URL',
    'OPENAI_BASE_URL',
    'OPENROUTER_BASE_URL',
    'OLLAMA_BASE_URL',
    'GROQ_BASE_URL',
    'XAI_BASE_URL'
)

try {
    New-Item -ItemType Directory -Force -Path $installDir, $userDataDir, $hermesHome, $workspace | Out-Null
    Write-SmokeLog "Installing $InstallerPath into $installDir"
    $install = Start-Process -FilePath $InstallerPath -ArgumentList @('/S', "/D=$installDir") -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "NSIS installer failed with exit code $($install.ExitCode)" }
    $installed = $true

    $stampPath = Join-Path $installDir 'resources\install-stamp.json'
    foreach ($required in @($exePath, $uninstallerPath, (Join-Path $installDir 'resources\app.asar'), $stampPath)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "Required installed file missing: $required"
        }
    }

    $versionInfo = (Get-Item -LiteralPath $exePath).VersionInfo
    if ($versionInfo.ProductName -ne 'Hermes' -or $versionInfo.FileDescription -ne 'Hermes') {
        throw "Wrong executable identity: ProductName=$($versionInfo.ProductName), FileDescription=$($versionInfo.FileDescription)"
    }
    if (-not $versionInfo.ProductVersion.StartsWith($ExpectedVersion, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installed version $($versionInfo.ProductVersion) does not match expected $ExpectedVersion"
    }

    $stamp = Get-Content -LiteralPath $stampPath -Raw | ConvertFrom-Json
    $stampCommit = [string]$stamp.commit
    if ([String]::IsNullOrWhiteSpace($stampCommit)) {
        throw 'Install stamp commit is empty'
    }
    if (-not $stampCommit.StartsWith($ExpectedCommit, [StringComparison]::OrdinalIgnoreCase) -and
        -not $ExpectedCommit.StartsWith($stampCommit, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Install stamp commit $stampCommit does not match workflow commit $ExpectedCommit"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $exePath
    Write-SmokeLog "Authenticode status (informational): $($signature.Status)"

    foreach ($entry in Get-ChildItem Env:) {
        if ($credentialEnvironmentNames -contains $entry.Name -or
            $entry.Name -match '(_API_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS|_ACCESS_KEY|_PRIVATE_KEY|_OAUTH_TOKEN)$') {
            $savedEnvironment[$entry.Name] = $entry.Value
            Remove-Item -LiteralPath "Env:$($entry.Name)"
        }
    }
    foreach ($name in $isolatedEnvironment.Keys) {
        if (Test-Path -LiteralPath "Env:$name") {
            $savedEnvironment[$name] = (Get-Item -LiteralPath "Env:$name").Value
        }
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        if ($null -ne $isolatedEnvironment[$name]) {
            Set-Item -LiteralPath "Env:$name" -Value $isolatedEnvironment[$name]
        }
    }

    if ($RunAuthE2E) {
        $repoRoot = Split-Path -Parent $PSScriptRoot
        Push-Location $repoRoot
        try {
            Write-SmokeLog 'Running installed Windows Playwright smoke'
            & npm.cmd run --workspace apps/desktop test:e2e:installed-windows-smoke 2>&1 |
                Tee-Object -FilePath $AuthLogPath -Append |
                ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) {
                throw "Installed Windows Playwright smoke failed with exit code $LASTEXITCODE"
            }

            Write-SmokeLog 'Running installed Windows auth lifecycle'
            & npm.cmd run --workspace apps/desktop test:e2e:installed-windows-auth 2>&1 |
                Tee-Object -FilePath $AuthLogPath -Append |
                ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) {
                throw "Installed Windows auth lifecycle failed with exit code $LASTEXITCODE"
            }
        } finally {
            Pop-Location
        }
    } elseif ($RunCredentialE2E) {
        $driverPath = Join-Path $PSScriptRoot 'desktop-credential-login.mjs'
        $nodePath = (Get-Command node.exe -ErrorAction Stop).Source
        $cdpPort = Get-AvailableLoopbackPort
        $credentialTimeoutMilliseconds = 45 * 60 * 1000

        Write-SmokeLog 'Launching installed Hermes for credential-backed acceptance'
        $startedProcess = Start-Process -FilePath $exePath -WorkingDirectory $workspace -ArgumentList @(
            '--remote-debugging-address=127.0.0.1',
            "--remote-debugging-port=$cdpPort",
            '--disable-gpu',
            '--no-sandbox'
        ) -PassThru
        Wait-ForCondition -TimeoutSeconds $StartupTimeoutSeconds `
            -FailureMessage 'Hermes credential acceptance window did not appear before timeout' -Condition {
            $startedProcess.Refresh()
            if ($startedProcess.HasExited) { throw "Hermes exited early with code $($startedProcess.ExitCode)" }
            return $startedProcess.MainWindowHandle -ne 0
        }
        Wait-ForCondition -TimeoutSeconds $StartupTimeoutSeconds `
            -FailureMessage 'Hermes credential diagnostic endpoint did not bind before timeout' -Condition {
            $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $cdpPort -ErrorAction SilentlyContinue)
            return $listeners.Count -gt 0
        }
        $foreignListeners = @(
            Get-NetTCPConnection -State Listen -LocalPort $cdpPort -ErrorAction Stop |
                Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }
        )
        if ($foreignListeners.Count -ne 0) {
            throw 'Hermes credential diagnostic endpoint was not loopback-only'
        }

        $driverInfo = [Diagnostics.ProcessStartInfo]::new()
        $driverInfo.FileName = $nodePath
        $driverInfo.ArgumentList.Add($driverPath)
        $driverInfo.ArgumentList.Add('--port')
        $driverInfo.ArgumentList.Add([string]$cdpPort)
        $driverInfo.ArgumentList.Add('--timeout-ms')
        $driverInfo.ArgumentList.Add([string]$credentialTimeoutMilliseconds)
        $driverInfo.UseShellExecute = $false
        $driverInfo.CreateNoWindow = $true
        $driverInfo.RedirectStandardInput = $true
        $driverInfo.RedirectStandardOutput = $true
        $driverInfo.RedirectStandardError = $true
        $driverInfo.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
        $driverInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
        $driverInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)

        $driver = [Diagnostics.Process]::new()
        $driver.StartInfo = $driverInfo
        if (-not $driver.Start()) {
            throw 'Credential login driver did not start'
        }
        $stdoutTask = $driver.StandardOutput.ReadToEndAsync()
        $stderrTask = $driver.StandardError.ReadToEndAsync()
        try {
            $driver.StandardInput.Write($credentialUsername)
            $driver.StandardInput.Write([char]0)
            $driver.StandardInput.Write($credentialPassword)
            $driver.StandardInput.Write([char]0)
        } finally {
            $driver.StandardInput.Close()
            $credentialUsername = ''
            $credentialPassword = ''
        }

        if (-not $driver.WaitForExit($credentialTimeoutMilliseconds + (32 * 60 * 1000))) {
            $driver.Kill($true)
            throw 'Credential login driver exceeded its bounded timeout'
        }
        $driver.WaitForExit()
        $driverOutput = ($stdoutTask.GetAwaiter().GetResult() + $stderrTask.GetAwaiter().GetResult()).Trim()
        if ($driverOutput) {
            $driverOutput | Set-Content -LiteralPath $AuthLogPath -Encoding UTF8
            $driverOutput -split "`r?`n" | ForEach-Object { Write-Host $_ }
        }
        if ($driver.ExitCode -ne 0) {
            throw "Credential-backed Windows acceptance failed with exit code $($driver.ExitCode)"
        }
        Write-SmokeLog 'Credential-backed login, runtime preparation, logout, and relock passed'
    } else {
        Write-SmokeLog 'Launching installed Hermes'
        $startedProcess = Start-Process -FilePath $exePath -WorkingDirectory $workspace -PassThru
        Wait-ForCondition -TimeoutSeconds $StartupTimeoutSeconds -FailureMessage 'Hermes window did not appear before timeout' -Condition {
            $startedProcess.Refresh()
            if ($startedProcess.HasExited) { throw "Hermes exited early with code $($startedProcess.ExitCode)" }
            return $startedProcess.MainWindowHandle -ne 0
        }
        Start-Sleep -Seconds $StabilitySeconds
        $startedProcess.Refresh()
        if ($startedProcess.HasExited) { throw 'Hermes exited during the stability interval' }
        Write-SmokeLog 'Hermes window is present and the process remained stable'
    }
} catch {
    $primaryFailure = $_
} finally {
    if ($null -ne $startedProcess) {
        try {
            $startedProcess.Refresh()
            if (-not $startedProcess.HasExited) {
                & taskkill.exe /PID $startedProcess.Id /T /F | ForEach-Object { Write-SmokeLog $_ }
            }
        } catch {
            $cleanupFailures.Add("process cleanup: $($_.Exception.Message)")
        }
    }

    if (($RunAuthE2E -or $RunCredentialE2E) -and (Test-Path -LiteralPath $installDir)) {
        $installPrefix = [IO.Path]::GetFullPath($installDir).TrimEnd('\') + '\'
        $cleanPasses = 0
        for ($sweep = 0; $sweep -lt 10 -and $cleanPasses -lt 3; $sweep++) {
            $found = 0
            try {
                Get-CimInstance Win32_Process -ErrorAction Stop |
                    Where-Object {
                        $_.ProcessId -ne $PID -and
                        $_.ExecutablePath -and
                        $_.ExecutablePath.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase)
                    } |
                    ForEach-Object {
                        $found++
                        $treePid = [string]$_.ProcessId
                        $previousErrorActionPreference = $ErrorActionPreference
                        $ErrorActionPreference = 'Continue'
                        try {
                            & taskkill.exe /PID $treePid /T /F 2>&1 |
                                ForEach-Object { Write-SmokeLog "$_" }
                        } finally {
                            $ErrorActionPreference = $previousErrorActionPreference
                            $global:LASTEXITCODE = 0
                        }
                    }
            } catch {
                $cleanupFailures.Add("process enumeration: $($_.Exception.Message)")
                break
            }
            if ($found -eq 0) { $cleanPasses++ } else { $cleanPasses = 0 }
            Start-Sleep -Milliseconds 400
        }
        if ($cleanPasses -lt 3) {
            $cleanupFailures.Add('installed Hermes processes survived bounded cleanup')
        }
    }

    if ($installed -and (Test-Path -LiteralPath $uninstallerPath -PathType Leaf)) {
        try {
            Write-SmokeLog 'Running silent uninstaller'
            $uninstall = Start-Process -FilePath $uninstallerPath -ArgumentList '/S' -Wait -PassThru
            if ($uninstall.ExitCode -ne 0) { throw "uninstaller exit code $($uninstall.ExitCode)" }
            Wait-ForCondition -TimeoutSeconds 30 -FailureMessage 'Installed program files remained after uninstall' -Condition {
                return -not (Test-Path -LiteralPath $exePath) -and -not (Test-Path -LiteralPath $uninstallerPath)
            }
        } catch {
            $cleanupFailures.Add("uninstall: $($_.Exception.Message)")
        }
    }

    foreach ($name in $isolatedEnvironment.Keys) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    foreach ($name in $savedEnvironment.Keys) {
        Set-Item -LiteralPath "Env:$name" -Value $savedEnvironment[$name]
    }
    $credentialUsername = ''
    $credentialPassword = ''
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($null -ne $primaryFailure) { throw $primaryFailure }
if ($cleanupFailures.Count -gt 0) { throw ($cleanupFailures -join '; ') }
Write-SmokeLog 'Windows NSIS install/launch/uninstall smoke passed'
