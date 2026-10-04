param(
    [ValidateSet("onedir", "onefile")][string]$Mode = "onedir",
    [string]$Python = "python",
    [int]$ReplaceRetries = 10
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"
$staging = Join-Path $dist (".AudioVTTForge-build-" + [guid]::NewGuid().ToString("N"))
$entry = Join-Path $root "audio_vtt_to_mp4_gui.py"
$hooks = Join-Path $root "hooks"
$runtimeHook = Join-Path $hooks "rthook-tkinter-dll.py"

if ($ReplaceRetries -lt 1) {
    throw "ReplaceRetries must be at least 1."
}

if ($Mode -eq "onefile") {
    $tmp = [System.IO.Path]::GetTempPath()
    try {
        $probe = Join-Path $tmp ("_avf_probe_" + [guid]::NewGuid().ToString("N"))
        [System.IO.Directory]::CreateDirectory($probe) | Out-Null
        [System.IO.Directory]::Delete($probe, $true)
    } catch {
        Write-Warning "TEMP is unavailable or not writable ($tmp). The onefile executable may fail to start; use the default onedir build."
    }
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null
New-Item -ItemType Directory -Force -Path $staging | Out-Null

try {
    $common = @(
        "--noconfirm",
        "--clean",
        "--name", "AudioVTTForge",
        "--distpath", $staging,
        "--workpath", $build,
        "--additional-hooks-dir", $hooks,
        "--runtime-hook", $runtimeHook
    )
    if ($Mode -eq "onedir") {
        & $Python -m PyInstaller @common --onedir --windowed $entry
    } else {
        & $Python -m PyInstaller @common --onefile --windowed $entry
    }
    if ($LASTEXITCODE -ne 0) {
        throw "EXE build failed during PyInstaller."
    }

    if ($Mode -eq "onefile") {
        $built = Join-Path $staging "AudioVTTForge.exe"
        $target = Join-Path $dist "AudioVTTForge.exe"
        if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
            throw "PyInstaller completed without creating $built."
        }

        $replaced = $false
        $lastError = $null
        for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
            try {
                if (Test-Path -LiteralPath $target -PathType Leaf) {
                    [System.IO.File]::Delete($target)
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
            throw "Could not replace $target after $ReplaceRetries attempts. The new EXE was saved to $fallback. Close the running app and retry. Last error: $($lastError.Exception.Message)"
        }
        $legacyDirectory = Join-Path $dist "AudioVTTForge"
        if (Test-Path -LiteralPath $legacyDirectory -PathType Container) {
            [System.IO.Directory]::Delete($legacyDirectory, $true)
        }
        Write-Output "EXE: $target"
    } else {
        $built = Join-Path $staging "AudioVTTForge"
        $target = Join-Path $dist "AudioVTTForge"
        if (-not (Test-Path -LiteralPath $built -PathType Container)) {
            throw "PyInstaller completed without creating $built."
        }

        $replaced = $false
        $lastError = $null
        for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
            $backup = Join-Path $dist (".AudioVTTForge-old-" + [guid]::NewGuid().ToString("N"))
            $backedUp = $false
            try {
                if (Test-Path -LiteralPath $target -PathType Container) {
                    [System.IO.Directory]::Move($target, $backup)
                    $backedUp = $true
                }
                [System.IO.Directory]::Move($built, $target)
                $replaced = $true
                if ($backedUp) {
                    try {
                        [System.IO.Directory]::Delete($backup, $true)
                    } catch {
                        Write-Warning "New build is ready; old build backup remains at ${backup}: $($_.Exception.Message)"
                    }
                }
                break
            } catch {
                $lastError = $_
                if ($backedUp -and -not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) {
                    [System.IO.Directory]::Move($backup, $target)
                }
                if ($attempt -lt $ReplaceRetries) {
                    Start-Sleep -Milliseconds ([Math]::Min(3000, 250 * $attempt))
                }
            }
        }

        if (-not $replaced) {
            $fallback = Join-Path $dist ("AudioVTTForge.new-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
            Copy-Item -LiteralPath $built -Destination $fallback -Recurse -Force
            throw "Could not replace $target after $ReplaceRetries attempts. The new build was saved to $fallback. Close the running app and retry. Last error: $($lastError.Exception.Message)"
        }
        $legacyOneFile = Join-Path $dist "AudioVTTForge.exe"
        if (Test-Path -LiteralPath $legacyOneFile -PathType Leaf) {
            [System.IO.File]::Delete($legacyOneFile)
        }
        Write-Output "EXE: $(Join-Path $target 'AudioVTTForge.exe')"
    }
} finally {
    if (Test-Path -LiteralPath $staging -PathType Container) {
        [System.IO.Directory]::Delete($staging, $true)
    }
}
