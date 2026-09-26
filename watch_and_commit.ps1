param(
    [int]$IntervalSeconds = 8,
    [switch]$Push
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if ($IntervalSeconds -lt 2) {
    throw "IntervalSeconds must be at least 2."
}

Write-Output "Watching $root"
Write-Output "Automatic commits are enabled. Press Ctrl+C to stop."

while ($true) {
    $status = @(git -C $root status --porcelain)
    if ($status.Count -gt 0) {
        & (Join-Path $root "auto_commit.ps1") -Push:$Push
    }
    Start-Sleep -Seconds $IntervalSeconds
}
