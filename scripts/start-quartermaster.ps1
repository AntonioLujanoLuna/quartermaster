[CmdletBinding()]
param(
    [string]$PidPath = (Join-Path $PSScriptRoot '..\quartermaster.pid'),
    [string]$LogDirectory = (Join-Path $PSScriptRoot '..\logs')
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stopPath = Join-Path $repo 'quartermaster.stop'

# Every QM_ value in the user environment, rather than a list of the ones that
# existed when this was written. The list this used to carry named only the five
# required values, so an optional setting configured user-level never reached the
# process — and an optional setting does not fail loudly. The Activity is the
# case that made it matter: without QM_DISCORD_CLIENT_ID and
# QM_DISCORD_CLIENT_SECRET the bot starts perfectly and simply does not serve it.
# Process values still win, so an ad-hoc shell can override for one run.
foreach ($entry in [Environment]::GetEnvironmentVariables('User').GetEnumerator()) {
    $name = [string]$entry.Key
    if ($name -notlike 'QM_*') {
        continue
    }
    if ([string]::IsNullOrWhiteSpace((Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value)) {
        if (-not [string]::IsNullOrWhiteSpace($entry.Value)) {
            Set-Item "Env:$name" $entry.Value
        }
    }
}

foreach ($name in @('QM_GUILD_ID', 'QM_PARTY_INVENTORY_CHANNEL_ID', 'QM_SESSION_LOG_CHANNEL_ID', 'QM_DATABASE_PATH', 'QM_DISCORD_TOKEN')) {
    if ([string]::IsNullOrWhiteSpace((Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value)) {
        throw "$name is not configured in the process or user environment"
    }
}

if (Test-Path -LiteralPath $PidPath) {
    $existingPid = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
    if ($existing -and (($existing.CommandLine -match 'quartermaster.*\brun\b') -or ($existing.CommandLine -match 'run-quartermaster-supervised\.ps1'))) {
        throw "Quartermaster is already running with PID $existingPid"
    }
    Remove-Item -LiteralPath $PidPath -Force
}

if (Test-Path -LiteralPath $stopPath) {
    Remove-Item -LiteralPath $stopPath -Force
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$stdout = Join-Path $LogDirectory 'quartermaster.stdout.log'
$stderr = Join-Path $LogDirectory 'quartermaster.stderr.log'
$supervisor = Join-Path $PSScriptRoot 'run-quartermaster-supervised.ps1'
$supervisorStdout = Join-Path $LogDirectory 'quartermaster.supervisor.stdout.log'
$supervisorStderr = Join-Path $LogDirectory 'quartermaster.supervisor.stderr.log'
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $supervisor, '-StopPath', $stopPath, '-Repo', $repo)
$process = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList $arguments `
    -WorkingDirectory $repo `
    -RedirectStandardOutput $supervisorStdout `
    -RedirectStandardError $supervisorStderr `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $PidPath -Value $process.Id -NoNewline
Write-Output "Quartermaster started with PID $($process.Id). Logs: $LogDirectory"
