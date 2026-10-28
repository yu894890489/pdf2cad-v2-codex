param(
    [Parameter(Mandatory = $true)][string]$Pdf,
    [string]$Out,
    [string]$TableRegions,
    [string]$Review,
    [string]$Config,
    [int]$Port = 8771,
    [switch]$NoServe
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
$resolvedPdf = (Resolve-Path -LiteralPath $Pdf).Path
$resolvedRegions = if ($TableRegions) { (Resolve-Path -LiteralPath $TableRegions).Path } else { $null }
$resolvedReview = if ($Review) { (Resolve-Path -LiteralPath $Review).Path } else { $null }
$resolvedConfig = if ($Config) { (Resolve-Path -LiteralPath $Config).Path } else { $null }
if (-not $Out) { $Out = 'runs/drawing-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8) }
Push-Location $PSScriptRoot
try {
    $pipelineArgs = @('-m', 'tower_rebuild', 'process', $resolvedPdf, '--out', $Out)
    if ($resolvedRegions) { $pipelineArgs += @('--table-regions', $resolvedRegions) }
    if ($resolvedReview) { $pipelineArgs += @('--review', $resolvedReview) }
    if ($resolvedConfig) { $pipelineArgs += @('--config', $resolvedConfig) }
    & $pythonExecutable @pipelineArgs
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Pipeline failed. Check $Out/workflow.json or the message above." }
    Write-Host "Results: $Out"
    $workflow = Get-Content -LiteralPath (Join-Path $Out 'workflow.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "Status: $($workflow.status)"
    if (Test-Path -LiteralPath (Join-Path $Out 'site/topology/model-preview.json')) {
        Write-Host "Drawing and 3D preview: http://127.0.0.1:$Port/topology/"
        Write-Host "Model dimension audit: http://127.0.0.1:$Port/model-audit/"
        Write-Host "Final check: http://127.0.0.1:$Port/final/"
    }
    if (-not $NoServe -and (Test-Path -LiteralPath (Join-Path $Out 'site/index.html'))) {
        & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port
        if ($LASTEXITCODE -ne 0) { throw "Local server failed (exit $LASTEXITCODE)." }
    }
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
}
