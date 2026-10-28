param(
    [string]$Pdf,
    [string]$Out = 'runs/new-drawing-decomposed',
    [string]$Review,
    [int]$Port = 8768,
    [switch]$NoServe
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
$resolvedPdf = if ($Pdf) { (Resolve-Path -LiteralPath $Pdf).Path } else { $null }
Push-Location $PSScriptRoot
try {
    if ($resolvedPdf) {
        & $pythonExecutable -m tower_rebuild decompose $resolvedPdf --out $Out
        if ($LASTEXITCODE -notin @(0, 2)) { throw "Decomposition failed. See $Out/failure.json" }
    }
    $topologyArgs = @('-m', 'tower_rebuild', 'topology', '--out', $Out)
    if ($Review) { $topologyArgs += @('--review', $Review) }
    & $pythonExecutable @topologyArgs
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Topology failed. See $Out/failure.json" }
    & $pythonExecutable -m tower_rebuild audit --out $Out
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Dimension audit failed. See $Out/failure.json" }
    & $pythonExecutable -m tower_rebuild preview --out $Out
    if ($LASTEXITCODE -notin @(0, 2)) { throw "3D preview failed. See $Out/failure.json" }
    Write-Host "Topology review: http://127.0.0.1:$Port/topology/"
    Write-Host "Dimension audit: http://127.0.0.1:$Port/audit/"
    Write-Host "Model dimension audit: http://127.0.0.1:$Port/model-audit/"
    Write-Host "Final check: http://127.0.0.1:$Port/final/"
    if (-not $NoServe) { & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port }
} finally {
    Pop-Location
}
