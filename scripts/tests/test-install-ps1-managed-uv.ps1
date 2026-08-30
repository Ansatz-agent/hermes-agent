# Product contract for the Windows managed-uv boundary.
#
# The packaged authentication bootstrap publishes a hash-verified uv.exe from
# bootstrap/auth-toolchain before install.ps1 runs the full runtime stages.
# install.ps1 may adopt that local executable, but it must never download or
# execute a remote installer on a clean machine.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts\install.ps1"
$installer = [System.IO.File]::ReadAllText($installScript)

$start = $installer.IndexOf("function Install-Uv")
$end = $installer.IndexOf("function Sync-EnvPath", $start)
if ($start -lt 0 -or $end -le $start) {
    throw "Could not locate the managed uv installer"
}
$installUv = $installer.Substring($start, $end - $start)

$required = @(
    '$managedUv = Join-Path $HermesHome "bin\uv.exe"',
    '$BundledToolchain',
    '$bundledUv = Join-Path $BundledToolchain "uv.exe"',
    'Managed uv adopted from the bundled toolchain',
    "Get-Command uv -CommandType Application",
    "Managed uv adopted from the local installation",
    "A verified uv payload is unavailable"
)
foreach ($text in $required) {
    if (-not $installUv.Contains($text)) {
        throw "Managed uv contract is missing: $text"
    }
}

if ($installUv -match '(?i)Invoke-WebRequest|Invoke-RestMethod|\birm\b|\biex\b|raw\.githubusercontent|astral\.sh') {
    throw "Managed uv installation must not download or execute remote code"
}
if ($installUv -match '(?i)https?://') {
    throw "Managed uv installation must remain local-only"
}

$testPythonStart = $installer.IndexOf("function Test-Python")
$testPythonEnd = $installer.IndexOf('$script:GitInstallFailureReason', $testPythonStart)
$testPython = $installer.Substring($testPythonStart, $testPythonEnd - $testPythonStart)
if ($testPython -notmatch 'Start-Process -FilePath \$UvCmd') {
    throw "Python provisioning must isolate uv output from PowerShell error records"
}
if ($testPython -notmatch '"--no-config", "python", "install"') {
    throw "Python provisioning must ignore ambient uv configuration"
}

Write-Host "Windows packaged managed uv contract passed." -ForegroundColor Green
