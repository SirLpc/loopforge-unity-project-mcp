[CmdletBinding()]
param(
    [string]$Config,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ExpectedBranch,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._/-]*$')][string]$Remote = 'origin'
)

$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
if (!$Config) { $Config = $env:UNITY_MCP_CONFIG }
if (!$Config) { $Config = Join-Path $serviceRoot 'config.toml' }
$python = Join-Path $serviceRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run scripts/install.ps1 first.' }
$manifest = & $python -m unity_project_mcp.server --config $Config --sync-info
if ($LASTEXITCODE -ne 0) { throw 'Could not validate service configuration.' }
$settings = $manifest | ConvertFrom-Json
$repo = $settings.repository_root
$gitExe = $settings.git
$rootFromGit = & $gitExe --no-optional-locks -C (Join-Path $repo $settings.unity_project_path) rev-parse --show-toplevel
if ($LASTEXITCODE -ne 0 -or !$rootFromGit -or
    [IO.Path]::GetFullPath($rootFromGit) -ne [IO.Path]::GetFullPath($repo)) {
    throw 'The configured reference is not the Git root of the Unity project.'
}

function Invoke-ReferenceGit([string[]]$GitArguments) {
    $output = & $gitExe --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=/dev/null -C $repo @GitArguments
    if ($LASTEXITCODE -ne 0) { throw "Git $($GitArguments[0]) failed ($LASTEXITCODE)." }
    return $output
}

function Assert-CleanExpectedBranch {
    $branch = Invoke-ReferenceGit @('branch', '--show-current')
    if (!$branch -or $branch -cne $ExpectedBranch) { throw "Expected branch '$ExpectedBranch' is not checked out." }
    $hidden = Invoke-ReferenceGit @('ls-files', '-v') | Where-Object { $_ -cnotmatch '^H ' } | Select-Object -First 1
    if ($hidden) { throw 'The reference Git index hides tracked changes.' }
    $changes = Invoke-ReferenceGit @('status', '--porcelain=v1', '--untracked-files=normal', '--ignore-submodules=none')
    if ($changes) { throw 'The reference worktree has tracked or untracked changes.' }
}

try { $mutex = [Threading.Mutex]::new($false, $settings.activity_name) }
catch { throw 'Cannot obtain the reference mutex. Stop the MCP service and any other reference sync before updating.' }
$locked = $false
$skipSmudge = $env:GIT_LFS_SKIP_SMUDGE
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (!$locked) { throw 'Stop the MCP service and any other reference sync before updating.' }
    $env:GIT_LFS_SKIP_SMUDGE = '1'
    Invoke-ReferenceGit @('check-ref-format', "refs/heads/$ExpectedBranch") | Out-Null
    $remotes = Invoke-ReferenceGit @('remote')
    if ($remotes -cnotcontains $Remote) { throw 'The requested remote is not configured.' }
    Assert-CleanExpectedBranch
    $before = Invoke-ReferenceGit @('rev-parse', '--verify', 'HEAD^{commit}')
    Write-Output "Before $before"

    Invoke-ReferenceGit @('-c', 'fetch.prune=false', '-c', 'fetch.pruneTags=false', 'fetch',
        '--no-tags', '--no-recurse-submodules', '--no-auto-maintenance', $Remote, "refs/heads/$ExpectedBranch") | Out-Null
    $target = Invoke-ReferenceGit @('rev-parse', '--verify', 'FETCH_HEAD^{commit}')
    Assert-CleanExpectedBranch
    if ((Invoke-ReferenceGit @('rev-parse', '--verify', 'HEAD^{commit}')) -cne $before) {
        throw 'The local HEAD changed during fetch.'
    }
    Invoke-ReferenceGit @('merge-base', '--is-ancestor', $before, $target) | Out-Null
    Invoke-ReferenceGit @('merge', '--ff-only', '--no-autostash', '--no-edit', '--no-overwrite-ignore', $target) | Out-Null
    $after = Invoke-ReferenceGit @('rev-parse', '--verify', 'HEAD^{commit}')
    Write-Output "After  $after"
} finally {
    $env:GIT_LFS_SKIP_SMUDGE = $skipSmudge
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
