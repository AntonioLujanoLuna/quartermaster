[CmdletBinding()]
param(
    [string]$PidPath = (Join-Path $PSScriptRoot '..\quartermaster.pid'),
    [string]$LogDirectory = (Join-Path $PSScriptRoot '..\logs')
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stopPath = Join-Path $repo 'quartermaster.stop'

foreach ($name in @('QM_GUILD_ID', 'QM_PARTY_INVENTORY_CHANNEL_ID', 'QM_SESSION_LOG_CHANNEL_ID', 'QM_DATABASE_PATH', 'QM_DISCORD_TOKEN')) {
    if ([string]::IsNullOrWhiteSpace((Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value)) {
        $value = [Environment]::GetEnvironmentVariable($name, 'User')
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            Set-Item "Env:$name" $value
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
