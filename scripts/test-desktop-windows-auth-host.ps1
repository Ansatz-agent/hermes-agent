[CmdletBinding(DefaultParameterSetName = 'Run')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Run')][string]$InstallerPath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Run')][string]$ExpectedCommit,
    [Parameter(Mandatory = $true, ParameterSetName = 'Run')][string]$ExpectedVersion,
    [Parameter(Mandatory = $true, ParameterSetName = 'Run')][string]$SmokeLogPath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Run')][string]$AuthLogPath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Preflight')][switch]$PreflightOnly,
    [Parameter(Mandatory = $true, ParameterSetName = 'Contract')][switch]$ContractSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TemporaryEnvironmentNames = @(
    'HERMES_E2E_AUTH_CERT_PATH',
    'HERMES_E2E_AUTH_KEY_PATH',
    'HERMES_E2E_WRONG_SAN_CERT_PATH',
    'HERMES_E2E_WRONG_SAN_KEY_PATH'
)

function Assert-NoForeignHostMapping([string]$HostsText) {
    foreach ($line in ($HostsText -split "`r?`n")) {
        if ($line -match '(?i)(^|\s)c2sml\.cn(\s|$)' -and
            $line -notmatch '(?i)#\s*hermes-auth-e2e\s*$') {
            throw 'hosts already contains a c2sml.cn mapping not owned by this test'
        }
    }
}

function Add-ManagedHostMapping([string]$HostsPath) {
    $bytes = [IO.File]::ReadAllBytes($HostsPath)
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    Assert-NoForeignHostMapping $text
    $separator = ''
    if ($bytes.Length -gt 0 -and -not ($text.EndsWith("`n") -or $text.EndsWith("`r"))) {
        $separator = "`r`n"
    }
    [IO.File]::AppendAllText(
        $HostsPath,
        "${separator}127.0.0.1 c2sml.cn # hermes-auth-e2e`r`n",
        [Text.UTF8Encoding]::new($false)
    )
}

function Assert-PortNotExcluded([string[]]$NetshLines, [int]$Port = 443) {
    foreach ($line in $NetshLines) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s*$') {
            $start = [int]$Matches[1]
            $end = [int]$Matches[2]
            if ($start -le $Port -and $Port -le $end) {
                throw "TCP port $Port is in an excluded Windows port range"
            }
        }
    }
}

function Assert-TcpPortBindable([int]$Port = 443) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
    } catch {
        throw "TCP port $Port cannot be bound on 127.0.0.1: $($_.Exception.Message)"
    } finally {
        try { $listener.Stop() } catch {}
    }
}

function Assert-ExclusiveDnsAddress([string[]]$Addresses) {
    $resolved = @($Addresses | Sort-Object -Unique)
    if ($resolved.Count -ne 1 -or $resolved[0] -ne '127.0.0.1') {
        throw 'c2sml.cn did not resolve exclusively to the local auth fixture'
    }
}

function Resolve-OpenSsl(
    [scriptblock]$Lookup = {
        $command = Get-Command openssl -ErrorAction SilentlyContinue
        if ($command) { $command.Source }
    },
    [string]$ProgramFilesRoot = $env:ProgramFiles
) {
    $openssl = & $Lookup
    if (-not $openssl) {
        $fallback = Join-Path $ProgramFilesRoot 'Git\usr\bin\openssl.exe'
        if (Test-Path -LiteralPath $fallback -PathType Leaf) { $openssl = $fallback }
    }
    if (-not $openssl) { throw 'OpenSSL is unavailable on this Windows runner' }
    return [IO.Path]::GetFullPath([string]$openssl)
}

function New-TestCertificate(
    [string]$OpenSslPath,
    [string]$CommonName,
    [string]$SanName,
    [string]$CertificatePath,
    [string]$KeyPath
) {
    & $OpenSslPath req -x509 -newkey rsa:2048 -nodes `
        -keyout $KeyPath -out $CertificatePath -days 1 `
        -subj "/CN=$CommonName" -addext "subjectAltName=DNS:$SanName" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0 -or
        -not (Test-Path -LiteralPath $CertificatePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
        throw "OpenSSL failed to create the $SanName test certificate"
    }
}

function Invoke-ContractSelfTest {
    if (-not $IsWindows) { throw 'Windows auth host contract self-test requires Windows' }
    $root = Join-Path $env:RUNNER_TEMP ("hermes-auth-host-contract-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    try {
        try {
            Resolve-OpenSsl -Lookup { $null } -ProgramFilesRoot $root | Out-Null
            throw 'missing OpenSSL contract unexpectedly passed'
        } catch {
            if ($_.Exception.Message -notmatch 'OpenSSL is unavailable') { throw }
        }

        try {
            Assert-PortNotExcluded @('  440  450')
            throw 'excluded port contract unexpectedly passed'
        } catch {
            if ($_.Exception.Message -notmatch 'excluded Windows port range') { throw }
        }

        $occupier = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 443)
        $occupier.Start()
        try {
            try {
                Assert-TcpPortBindable 443
                throw 'occupied port contract unexpectedly passed'
            } catch {
                if ($_.Exception.Message -notmatch 'cannot be bound') { throw }
            }
        } finally {
            $occupier.Stop()
        }

        try {
            Assert-NoForeignHostMapping "127.0.0.2 c2sml.cn`r`n"
            throw 'foreign hosts mapping contract unexpectedly passed'
        } catch {
            if ($_.Exception.Message -notmatch 'not owned by this test') { throw }
        }

        try {
            Assert-ExclusiveDnsAddress @('127.0.0.1', '::1')
            throw 'non-exclusive DNS contract unexpectedly passed'
        } catch {
            if ($_.Exception.Message -notmatch 'did not resolve exclusively') { throw }
        }

        $hostsFixture = Join-Path $root 'hosts'
        $original = [Text.Encoding]::ASCII.GetBytes("127.0.0.1 localhost`r`n")
        [IO.File]::WriteAllBytes($hostsFixture, $original)
        foreach ($name in $TemporaryEnvironmentNames) {
            Set-Item -LiteralPath "Env:$name" -Value (Join-Path $root "$name.pem")
        }
        try {
            Add-ManagedHostMapping $hostsFixture
            throw 'simulated install subcommand failure'
        } catch {
            if ($_.Exception.Message -notmatch 'simulated install subcommand failure') { throw }
        } finally {
            [IO.File]::WriteAllBytes($hostsFixture, $original)
            foreach ($name in $TemporaryEnvironmentNames) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
        }
        $restored = [IO.File]::ReadAllBytes($hostsFixture)
        if ([Convert]::ToBase64String($restored) -ne [Convert]::ToBase64String($original)) {
            throw 'hosts fixture was not restored byte-for-byte after simulated install failure'
        }
        foreach ($name in $TemporaryEnvironmentNames) {
            if (Test-Path -LiteralPath "Env:$name") {
                throw "$name survived contract self-test cleanup"
            }
        }
    } finally {
        Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host 'Windows auth host contract self-test passed'
}

if ($ContractSelfTest) {
    Invoke-ContractSelfTest
    exit 0
}

if (-not $IsWindows) { throw 'Windows auth host harness requires Windows' }
$hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
if (-not (Test-Path -LiteralPath $hostsPath -PathType Leaf)) {
    throw "Windows hosts file not found: $hostsPath"
}

$runnerTemp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$testRoot = Join-Path $runnerTemp ("hermes-auth-host-" + [Guid]::NewGuid().ToString('N'))
$backupPath = Join-Path $testRoot 'hosts.backup'
$certificatePath = Join-Path $testRoot 'c2sml-cert.pem'
$keyPath = Join-Path $testRoot 'c2sml-key.pem'
$wrongCertificatePath = Join-Path $testRoot 'wrong-san-cert.pem'
$wrongKeyPath = Join-Path $testRoot 'wrong-san-key.pem'
$originalHostsBytes = $null
$thumbprints = [Collections.Generic.List[string]]::new()
$primaryFailure = $null
$cleanupFailures = [Collections.Generic.List[string]]::new()

try {
    New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
    $originalHostsBytes = [IO.File]::ReadAllBytes($hostsPath)
    [IO.File]::WriteAllBytes($backupPath, $originalHostsBytes)
    Assert-NoForeignHostMapping ([Text.Encoding]::UTF8.GetString($originalHostsBytes))

    $excludedRanges = @(& netsh.exe int ipv4 show excludedportrange protocol=tcp)
    $excludedRanges | ForEach-Object { Write-Host $_ }
    Assert-PortNotExcluded $excludedRanges
    Assert-TcpPortBindable 443

    $openssl = Resolve-OpenSsl
    New-TestCertificate $openssl 'c2sml.cn' 'c2sml.cn' $certificatePath $keyPath
    New-TestCertificate $openssl 'not-c2sml.invalid' 'not-c2sml.invalid' $wrongCertificatePath $wrongKeyPath
    foreach ($generatedCertificate in @($certificatePath, $wrongCertificatePath)) {
        $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($generatedCertificate)
        try { $thumbprints.Add($certificate.Thumbprint) } finally { $certificate.Dispose() }
    }

    Add-ManagedHostMapping $hostsPath
    ipconfig.exe /flushdns | Out-Null
    $resolved = @([Net.Dns]::GetHostAddresses('c2sml.cn') |
        ForEach-Object { $_.IPAddressToString } | Sort-Object -Unique)
    Assert-ExclusiveDnsAddress $resolved

    $env:HERMES_E2E_AUTH_CERT_PATH = $certificatePath
    $env:HERMES_E2E_AUTH_KEY_PATH = $keyPath
    $env:HERMES_E2E_WRONG_SAN_CERT_PATH = $wrongCertificatePath
    $env:HERMES_E2E_WRONG_SAN_KEY_PATH = $wrongKeyPath

    if (-not $PreflightOnly) {
        $installScript = Join-Path $PSScriptRoot 'test-desktop-windows-install.ps1'
        & pwsh -NoProfile -ExecutionPolicy Bypass -File $installScript `
            -InstallerPath $InstallerPath `
            -ExpectedCommit $ExpectedCommit `
            -ExpectedVersion $ExpectedVersion `
            -LogPath $SmokeLogPath `
            -RunAuthE2E `
            -AuthLogPath $AuthLogPath
        if ($LASTEXITCODE -ne 0) {
            throw "Windows installed auth E2E failed with exit code $LASTEXITCODE"
        }
    }
} catch {
    $primaryFailure = $_
} finally {
    foreach ($name in $TemporaryEnvironmentNames) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }

    if ($null -ne $originalHostsBytes) {
        try {
            [IO.File]::WriteAllBytes($hostsPath, $originalHostsBytes)
            ipconfig.exe /flushdns | Out-Null
            $restoredHostsBytes = [IO.File]::ReadAllBytes($hostsPath)
            if ([Convert]::ToBase64String($restoredHostsBytes) -ne
                [Convert]::ToBase64String($originalHostsBytes)) {
                throw 'Windows hosts file was not restored byte-for-byte'
            }
        } catch {
            $cleanupFailures.Add("hosts restore: $($_.Exception.Message)")
        }
    }

    try {
        $rootMatches = @(
            Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root -ErrorAction Stop |
                Where-Object { $thumbprints -contains $_.Thumbprint }
        )
        if ($rootMatches.Count -ne 0) {
            throw 'temporary auth certificate appeared in a Windows Root store'
        }
    } catch {
        $cleanupFailures.Add("Root store check: $($_.Exception.Message)")
    }

    try {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $testRoot) {
            throw 'temporary auth host directory survived cleanup'
        }
    } catch {
        $cleanupFailures.Add("temporary directory cleanup: $($_.Exception.Message)")
    }
}

if ($null -ne $primaryFailure) { throw $primaryFailure }
if ($cleanupFailures.Count -gt 0) { throw ($cleanupFailures -join '; ') }
if ($PreflightOnly) {
    Write-Host 'Windows fixed-origin auth host preflight passed and rolled back'
}
