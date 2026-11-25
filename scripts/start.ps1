param([string]$Config)

$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
if (!$Config) { $Config = $env:UNITY_MCP_CONFIG }
if (!$Config) { $Config = Join-Path $serviceRoot 'config.toml' }
if (!$env:UNITY_MCP_TOKEN) { throw 'Set UNITY_MCP_TOKEN in the service environment.' }
$python = Join-Path $serviceRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run scripts/install.ps1 first.' }
& $python -m unity_project_mcp.server --config $Config
exit $LASTEXITCODE
