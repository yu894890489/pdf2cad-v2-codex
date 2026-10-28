param(
    [Parameter(Mandatory = $true)][string]$Pdf,
    [string]$Out,
    [int]$Port = 8768
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
$resolvedPdf = (Resolve-Path -LiteralPath $Pdf).Path
if (-not $Out) {
    $runName = [IO.Path]::GetFileNameWithoutExtension($resolvedPdf)
    $Out = Join-Path $PSScriptRoot "runs/$runName-decomposed"
}
Push-Location $PSScriptRoot
try {
    & $pythonExecutable -m tower_rebuild decompose $resolvedPdf --out $Out
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Decomposition failed. See $Out/failure.json" }
    & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port
} finally {
    Pop-Location
}
