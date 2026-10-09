param(
    [ValidateSet("gui", "web")][string]$Target = "gui",
    [ValidateSet("onedir", "onefile")][string]$Mode = "onedir",
    [string]$Python = "python",
    [int]$ReplaceRetries = 10
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"
$staging = Join-Path $dist (".AudioVTTForge-build-" + [guid]::NewGuid().ToString("N"))
$hooks = Join-Path $root "hooks"
$runtimeHook = Join-Path $hooks "rthook-tkinter-dll.py"

# gui : the Tk desktop application (windowed, no console).
# web : the local REST API + browser UI as a console app. The console window is
#       how the user stops the service, so --windowed must stay off; web_static
#       has to ship as data or /assets/* would 404 in the frozen build.
if ($Target -eq "web") {
    $name = "AudioVTTForgeWeb"
    $entry = Join-Path $root "audiovttforge\web_entry.py"
    $extra = @("--add-data", "$root\audiovttforge\web_static;audiovttforge\web_static")
    $windowArgs = @()
} else {
    $name = "AudioVTTForge"
    $entry = Join-Path $root "audio_vtt_to_mp4_gui.py"
    $extra = @()
    $windowArgs = @("--windowed")
}

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
        "--name", $name,
        "--distpath", $staging,
        "--workpath", $build,
        "--additional-hooks-dir", $hooks,
        "--runtime-hook", $runtimeHook
    ) + $extra
    if ($Mode -eq "onedir") {
        & $Python -m PyInstaller @common --onedir @windowArgs $entry
    } else {
        & $Python -m PyInstaller @common --onefile @windowArgs $entry
    }
    if ($LASTEXITCODE -ne 0) {
        throw "EXE build failed during PyInstaller."
    }

    if ($Mode -eq "onefile") {
        $built = Join-Path $staging "$name.exe"
        $targetPath = Join-Path $dist "$name.exe"
        if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
            throw "PyInstaller completed without creating $built."
        }

        $replaced = $false
        $lastError = $null
        for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
            try {
                if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
                    [System.IO.File]::Delete($targetPath)
                }
                Move-Item -LiteralPath $built -Destination $targetPath -Force -ErrorAction Stop
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
            $fallback = Join-Path $dist ("$name.new-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".exe")
            Copy-Item -LiteralPath $built -Destination $fallback -Force
            throw "Could not replace $targetPath after $ReplaceRetries attempts. The new EXE was saved to $fallback. Close the running app and retry. Last error: $($lastError.Exception.Message)"
        }
        $legacyDirectory = Join-Path $dist $name
        if (Test-Path -LiteralPath $legacyDirectory -PathType Container) {
            [System.IO.Directory]::Delete($legacyDirectory, $true)
        }
        Write-Output "EXE: $targetPath"
    } else {
        $built = Join-Path $staging $name
        $targetPath = Join-Path $dist $name
        if (-not (Test-Path -LiteralPath $built -PathType Container)) {
            throw "PyInstaller completed without creating $built."
        }

        $replaced = $false
        $lastError = $null
        for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
            $backup = Join-Path $dist (".$name-old-" + [guid]::NewGuid().ToString("N"))
            $backedUp = $false
            try {
                if (Test-Path -LiteralPath $targetPath -PathType Container) {
                    [System.IO.Directory]::Move($targetPath, $backup)
                    $backedUp = $true
                }
                [System.IO.Directory]::Move($built, $targetPath)
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
                if ($backedUp -and -not (Test-Path -LiteralPath $targetPath) -and (Test-Path -LiteralPath $backup)) {
                    [System.IO.Directory]::Move($backup, $targetPath)
                }
                if ($attempt -lt $ReplaceRetries) {
                    Start-Sleep -Milliseconds ([Math]::Min(3000, 250 * $attempt))
                }
            }
        }

        if (-not $replaced) {
            $fallback = Join-Path $dist ("$name.new-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
            Copy-Item -LiteralPath $built -Destination $fallback -Recurse -Force
            throw "Could not replace $targetPath after $ReplaceRetries attempts. The new build was saved to $fallback. Close the running app and retry. Last error: $($lastError.Exception.Message)"
        }
        $legacyOneFile = Join-Path $dist "$name.exe"
        if (Test-Path -LiteralPath $legacyOneFile -PathType Leaf) {
            [System.IO.File]::Delete($legacyOneFile)
        }
        Write-Output "EXE: $(Join-Path $targetPath "$name.exe")"
    }
} finally {
    if (Test-Path -LiteralPath $staging -PathType Container) {
        [System.IO.Directory]::Delete($staging, $true)
    }
}
