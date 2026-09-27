param(
    [string]$Python = "python",
    [int]$ReplaceRetries = 10
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("AudioVTTForge-build-" + [guid]::NewGuid().ToString("N"))
$target = Join-Path $dist "AudioVTTForge.exe"

if ($ReplaceRetries -lt 1) {
    throw "ReplaceRetries must be at least 1."
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
try {
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "AudioVTTForge" `
        --distpath $staging `
        --workpath $build `
        (Join-Path $root "audio_vtt_to_mp4_gui.py")
    if ($LASTEXITCODE -ne 0) {
        throw "EXE build failed during PyInstaller."
    }

    $built = Join-Path $staging "AudioVTTForge.exe"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
        throw "PyInstaller completed without creating $built."
    }

    $replaced = $false
    $lastError = $null
    for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
        try {
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Force -ErrorAction Stop
            }
            Move-Item -LiteralPath $built -Destination $target -Force -ErrorAction Stop
            $replaced = $true
            break
        } catch {
            $lastError = $_
            if ($attempt -lt $ReplaceRetries) {
                Start-Sleep -Milliseconds ([Math]::Min(3000, 250 * $attempt))
            }
        }
    }

    if (-not $replaced) {
        $fallback = Join-Path $dist ("AudioVTTForge.new-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".exe")
        Copy-Item -LiteralPath $built -Destination $fallback -Force
        throw "Could not replace $target after $ReplaceRetries attempts. The new EXE was saved to $fallback. Close the running app or any program using the old EXE, then replace it manually. Last error: $($lastError.Exception.Message)"
    }

    Write-Output "EXE: $target"
} finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
