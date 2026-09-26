param(
    [string]$Message = "",
    [switch]$Push
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

git -C $root add -A
$changes = @(git -C $root status --porcelain)
if ($changes.Count -eq 0) {
    Write-Output "No changes to commit."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Message)) {
    $Message = "auto: update $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}

git -C $root commit -m $Message
if ($LASTEXITCODE -ne 0) {
    throw "Git commit failed."
}

if ($Push) {
    git -C $root push
    if ($LASTEXITCODE -ne 0) {
        throw "Git push failed."
    }
}
