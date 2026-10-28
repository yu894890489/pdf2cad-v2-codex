param(
    [Parameter(Mandatory = $true)][string]$Pdf,
    [string]$Out,
    [string]$Config,
    [int]$Port = 8766
)
$ErrorActionPreference = 'Stop'
if (-not $Config) {
    $Config = Join-Path $PSScriptRoot 'examples/qwen.aliyun.json'
}
# Keep this launcher ASCII-only: Windows PowerShell 5.1 reads BOM-less UTF-8
# using the legacy ANSI code page, which can corrupt quoted Chinese strings.
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
$resolvedPdf = (Resolve-Path -LiteralPath $Pdf).Path
if (-not $Out) {
    $runName = [IO.Path]::GetFileNameWithoutExtension($resolvedPdf)
    $Out = Join-Path $PSScriptRoot "runs/$runName"
}
Push-Location $PSScriptRoot
try {
    & $pythonExecutable -m tower_rebuild run $resolvedPdf --out $Out --config $Config
    $pipelineExit = $LASTEXITCODE
    if ($pipelineExit -notin @(0, 2)) { throw "Pipeline failed (exit $pipelineExit). See $Out/failure.json" }
    Write-Host "Local viewer: http://127.0.0.1:$Port/ (Ctrl+C to stop)"
    & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port
} finally {
    Pop-Location
}
