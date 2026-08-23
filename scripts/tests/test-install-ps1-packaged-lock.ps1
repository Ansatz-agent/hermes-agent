$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot 'scripts\install.ps1'
$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$testRoot = Join-Path $tempRoot ("hermes-packaged-lock-" + [Guid]::NewGuid().ToString('N'))
$hermesHome = Join-Path $testRoot 'home'
$installRoot = Join-Path $hermesHome 'hermes-agent'

try {
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

    # Build the managed-tool precondition through the installer itself. This
    # is the same stage boundary the packaged desktop drives and avoids
    # manufacturing a trusted state by copying an unrelated PATH executable.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $uvOutput = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $installScript `
        -Stage uv `
        -NonInteractive `
        -Json `
        -InstallDir $installRoot `
        -HermesHome $hermesHome 2>&1
    $uvExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($uvExitCode -ne 0) {
        throw "Managed uv bootstrap failed before packaged lock test: $($uvOutput | Out-String)"
    }

    Set-Content -LiteralPath (Join-Path $installRoot 'pyproject.toml') -Value @(
        '[project]'
        'name = "fixture"'
        'version = "0.0.0"'
        'requires-python = ">=3.11"'
        'dependencies = []'
        ''
        '[project.optional-dependencies]'
        'all = []'
    ) -Encoding ASCII
    Copy-Item -LiteralPath (Join-Path $repoRoot 'uv.lock') -Destination (Join-Path $installRoot 'uv.lock')

    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $installScript `
        -Stage dependencies `
        -NonInteractive `
        -Json `
        -BundledSource `
        -InstallDir $installRoot `
        -HermesHome $hermesHome 2>&1
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        throw 'Bundled dependency stage unexpectedly accepted a failed locked sync'
    }

    $jsonLine = @($output | ForEach-Object { "$_" } | Where-Object { $_.TrimStart().StartsWith('{') })[-1]
    $frame = $jsonLine | ConvertFrom-Json
    if ($frame.ok -ne $false -or $frame.stage -ne 'dependencies') {
        throw "Bundled dependency stage returned an invalid failure frame: $jsonLine"
    }
    if ([string]$frame.reason -notmatch 'locked dependency installation failed') {
        throw "Bundled dependency failure was not fail-closed: $($frame.reason)"
    }

    if (($output | Out-String) -match 'uv.lock sync failed.+falling back to PyPI resolve') {
        throw 'Bundled dependency stage attempted an unlocked PyPI fallback'
    }
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
