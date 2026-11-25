param([string]$Python = '3.11')

$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -CommandType Application -ErrorAction Stop).Source
Get-Command git, rg -CommandType Application -ErrorAction Stop | Out-Null
& $uv sync --project $serviceRoot --locked --python $Python
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed ($LASTEXITCODE)." }
