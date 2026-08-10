[CmdletBinding()]
param(
    [string]$PidPath = (Join-Path $PSScriptRoot '..\quartermaster.pid')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Output 'Quartermaster is not recorded as running.'
    exit 0
}

$pidValue = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $PidPath -Force
    Write-Output "Removed stale PID file for $pidValue."
    exit 0
}
if ($process.CommandLine -notmatch 'quartermaster.*\brun\b') {
    throw "Refusing to stop PID $pidValue because it is not a Quartermaster process"
}

$allProcesses = @(Get-CimInstance Win32_Process)
$tree = New-Object System.Collections.Generic.List[int]
$tree.Add($pidValue)
$queue = New-Object System.Collections.Generic.Queue[int]
$queue.Enqueue($pidValue)
while ($queue.Count -gt 0) {
    $parent = $queue.Dequeue()
    foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $parent -and $_.CommandLine -match 'quartermaster.*\brun\b' }) {
        if (-not $tree.Contains([int]$child.ProcessId)) {
            $tree.Add([int]$child.ProcessId)
            $queue.Enqueue([int]$child.ProcessId)
        }
    }
}

foreach ($targetPid in ($tree | Sort-Object -Descending)) {
    Stop-Process -Id $targetPid -ErrorAction SilentlyContinue
}
foreach ($targetPid in $tree) {
    Wait-Process -Id $targetPid -Timeout 10 -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $PidPath -Force
Write-Output "Quartermaster process tree stopped (root PID $pidValue)."
