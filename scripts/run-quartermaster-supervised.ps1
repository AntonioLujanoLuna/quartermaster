[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StopPath,
    [string]$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path $Repo).Path
$stopFile = [System.IO.Path]::GetFullPath($StopPath)
$stdout = Join-Path $repo 'logs\quartermaster.stdout.log'
$stderr = Join-Path $repo 'logs\quartermaster.stderr.log'
$supervisorLog = Join-Path $repo 'logs\quartermaster.supervisor.log'
$arguments = @('run', 'python', '-m', 'quartermaster', '--db', $env:QM_DATABASE_PATH, 'run')

while (-not (Test-Path -LiteralPath $stopFile)) {
    $child = $null
    try {
        $child = Start-Process `
            -FilePath 'uv' `
            -ArgumentList $arguments `
            -WorkingDirectory $repo `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -WindowStyle Hidden `
            -PassThru
        $child | Wait-Process
        $exitCode = $child.ExitCode
    }
    catch {
        $exitCode = -1
        Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format o) supervisor failed to start Quartermaster: $($_.Exception.Message)"
    }

    if (Test-Path -LiteralPath $stopFile) {
        break
    }

    Add-Content -LiteralPath $supervisorLog -Value "$(Get-Date -Format o) Quartermaster exited with code $exitCode; restarting in 5 seconds."
    Start-Sleep -Seconds 5
}

if (Test-Path -LiteralPath $stopFile) {
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
}
