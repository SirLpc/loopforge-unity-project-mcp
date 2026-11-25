$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot
$deployment = Join-Path $env:LOCALAPPDATA 'LoopForge\UnityProjectMCP'
$launcher = Join-Path $deployment 'start.ps1'
$config = Join-Path $deployment 'config.toml'
$python = Join-Path $source '.venv\Scripts\python.exe'

foreach ($file in @($launcher, $config, $python, (Join-Path $deployment 'token.dpapi'))) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required deployment file is missing: $file" }
}

$settingsProbe = @'
from pathlib import Path
from sys import argv
from unity_project_mcp.server import load_settings

settings = load_settings(Path(argv[1]))
settings.locations()
print(settings.server.host, settings.server.port)
'@
$address = & $python -c $settingsProbe $config
if ($LASTEXITCODE -ne 0 -or $address -notmatch '^127\.0\.0\.1 (?<port>[0-9]+)$') {
    throw 'The deployment must have valid settings and listen on 127.0.0.1.'
}
$port = [int]$Matches.port

$escapedConfig = [regex]::Escape($config)
$serverCommand = '^(?:"[^"]+"|\S+)\s+-m\s+unity_project_mcp\.server\s+--config\s+(?:"' + $escapedConfig + '"|' + $escapedConfig + ')\s*$'

function Get-McpListener {
    $owners = @(Get-NetTCPConnection -State Listen | Where-Object {
        $_.LocalPort -eq $port -and $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::')
    } | Select-Object -ExpandProperty OwningProcess -Unique)
    if (!$owners.Count) { return }
    if ($owners.Count -ne 1) { throw "Multiple processes are listening on port $port." }

    $ownerId = $owners[0]
    $server = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId"
    $parent = $null
    if ($server.CommandLine -match $serverCommand -and $server.ExecutablePath -ne $python) {
        # A Windows venv redirector can launch the Python process that actually owns the socket.
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($server.ParentProcessId)"
    }
    if ($server.CommandLine -notmatch $serverCommand -or
        ($server.ExecutablePath -ne $python -and
         ($parent.ExecutablePath -ne $python -or $parent.CommandLine -notmatch $serverCommand -or
          $parent.CreationDate -gt $server.CreationDate))) {
        throw "Port $port is listening under an unrelated process (PID $ownerId)."
    }

    $process = Get-Process -Id $ownerId
    if ([Math]::Abs(($process.StartTime - $server.CreationDate).TotalMilliseconds) -gt 1) {
        throw "The process listening on port $port changed during inspection. Run the script again."
    }
    return $process
}

$restartLock = [IO.File]::Open((Join-Path $deployment 'restart.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    $current = Get-McpListener
    if ($current) {
        Stop-Process -InputObject $current
        Wait-Process -InputObject $current -Timeout 10
    }

    $stderrLog = Join-Path $deployment 'restart.stderr.log'
    $stdoutLog = Join-Path $deployment 'restart.stdout.log'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $started = Start-Process -FilePath $powershell `
        -ArgumentList ('-NoProfile -NonInteractive -File "' + $launcher + '"') `
        -WorkingDirectory $source -WindowStyle Hidden -PassThru `
        -RedirectStandardError $stderrLog -RedirectStandardOutput $stdoutLog

    $elapsed = [Diagnostics.Stopwatch]::StartNew()
    do {
        if ($started.HasExited) { throw "MCP launcher exited before listening. See $stderrLog and $deployment\state\service.log." }
        $ready = Get-McpListener
        if ($ready) {
            Write-Output "MCP is listening at http://127.0.0.1:$port/mcp (PID $($ready.Id))."
            return
        }
        Start-Sleep -Milliseconds 500
    } while ($elapsed.Elapsed.TotalSeconds -lt 45)
    throw "MCP did not listen within 45 seconds. See $stderrLog and $deployment\state\service.log."
} finally {
    $restartLock.Dispose()
}
