# Contract for optional Windows system-package provisioning.
#
# ripgrep and ffmpeg improve optional features, but a stalled package manager
# must never abort the desktop bootstrap. Keep this contract text-based so it
# can run on the packaging host as well as on Windows CI without invoking an
# actual system package manager.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts\install.ps1"
$installer = [System.IO.File]::ReadAllText($installScript)

$helperStart = $installer.IndexOf("function Invoke-OptionalPackageManager")
$helperEnd = $installer.IndexOf("function Install-SystemPackages", $helperStart)
if ($helperStart -lt 0 -or $helperEnd -le $helperStart) {
    throw "Optional package-manager helper is missing"
}
$helper = $installer.Substring($helperStart, $helperEnd - $helperStart)

foreach ($required in @(
    'Start-Process -FilePath $launcher',
    'RedirectStandardOutput $stdoutPath',
    'RedirectStandardError $stderrPath',
    'while (-not $proc.WaitForExit(750))',
    'package installation still running',
    'taskkill /T /F /PID $proc.Id',
    'TimedOut = $timedOut',
    'ExitCode = if ($timedOut) { 124 }'
)) {
    if (-not $helper.Contains($required)) {
        throw "Optional package-manager timeout contract is missing: $required"
    }
}

$stageStart = $installer.IndexOf("function Install-SystemPackages")
$stageEnd = $installer.IndexOf("function Assert-BundledSource", $stageStart)
if ($stageEnd -le $stageStart) {
    throw "Could not locate Install-SystemPackages body"
}
$stage = $installer.Substring($stageStart, $stageEnd - $stageStart)

$nodeStart = $installer.IndexOf("function Test-Node")
$nodeEnd = $installer.IndexOf("function Update-ProcessPathForPackages", $nodeStart)
if ($nodeStart -lt 0 -or $nodeEnd -le $nodeStart) {
    throw "Could not locate the Windows Node fallback body"
}
$nodeFallback = $installer.Substring($nodeStart, $nodeEnd - $nodeStart)

if (-not $helper.Contains('HERMES_SYSTEM_PACKAGE_TIMEOUT') -or
    -not $stage.Contains('Get-OptionalPackageTimeoutSec')) {
    throw "System package stage must use the shared bounded timeout resolver"
}
if ($stage -notmatch 'Invoke-OptionalPackageManager\s+-Manager\s+''winget''') {
    throw "winget must use the bounded optional package-manager helper"
}
if ($stage -notmatch 'Invoke-OptionalPackageManager\s+-Manager\s+''choco''') {
    throw "Chocolatey must use the bounded optional package-manager helper"
}
if ($stage -notmatch 'Invoke-OptionalPackageManager\s+-Manager\s+''scoop''') {
    throw "Scoop must use the bounded optional package-manager helper"
}
if (-not $stage.Contains('Update-WingetSource') -or
    -not $stage.Contains('--disable-interactivity')) {
    throw "winget must refresh and use the configured non-interactive source"
}
if ($nodeFallback -notmatch 'Invoke-OptionalPackageManager\s+-Manager\s+''winget''') {
    throw "Node's winget fallback must use the bounded optional package-manager helper"
}
if ($nodeFallback.Contains('winget @wingetArgs')) {
    throw "Node fallback contains an unbounded winget invocation"
}
if ($stage -match '(?m)^\s*(winget|choco|scoop)\s+install\b') {
    throw "System package stage contains an unbounded package-manager invocation"
}

Write-Host "Windows optional system-package timeout contract passed." -ForegroundColor Green
