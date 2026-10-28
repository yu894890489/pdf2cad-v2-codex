param(
    [string]$Root = 'runs/studio',
    [int]$Port = 8770,
    [string]$Config
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
$resolvedConfig = if ($Config) { (Resolve-Path -LiteralPath $Config).Path } else { $null }
Push-Location $PSScriptRoot
try {
    $studioArgs = @('-m', 'tower_rebuild', 'studio', '--root', $Root, '--port', $Port)
    if ($resolvedConfig) { $studioArgs += @('--config', $resolvedConfig) }
    & $pythonExecutable @studioArgs
    if ($LASTEXITCODE -ne 0) { throw "Local studio failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
