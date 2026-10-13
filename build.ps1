param(
    [ValidateSet("onedir", "onefile")][string]$Mode = "onedir",
    [string]$Python = "",
    [int]$ReplaceRetries = 10
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$dist = Join-Path $root "dist"
$runRoot = Join-Path $root ("tmp\build\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$work = Join-Path $runRoot "work"
$staging = Join-Path $runRoot "staging"

if (-not $Python) {
    if ($env:AUDIOVTTFORGE_PYTHON) {
        $Python = $env:AUDIOVTTFORGE_PYTHON
    } elseif (Test-Path -LiteralPath "D:\tools\audiovttforge-desktop\Scripts\python.exe" -PathType Leaf) {
        $Python = "D:\tools\audiovttforge-desktop\Scripts\python.exe"
    } else {
        $Python = "python"
    }
}
if ($ReplaceRetries -lt 1) {
    throw "ReplaceRetries must be at least 1."
}

function Assert-ProjectPath([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $prefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside the project: $resolved"
    }
    return $resolved
}

function Move-ProjectItem([string]$Source, [string]$Destination) {
    $safeSource = Assert-ProjectPath $Source
    $safeDestination = Assert-ProjectPath $Destination
    Move-Item -LiteralPath $safeSource -Destination $safeDestination -ErrorAction Stop
}

function Recycle-ProjectItem([string]$Path) {
    $resolved = Assert-ProjectPath $Path
    if (-not (Test-Path -LiteralPath $resolved)) { return }
    Add-Type -AssemblyName Microsoft.VisualBasic
    if (Test-Path -LiteralPath $resolved -PathType Container) {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
            $resolved,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
            [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
        )
    } else {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $resolved,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
            [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
        )
    }
}

# The desktop application is the only packaged UI.
$extra = @("--add-data", "$root\audiovttforge\web_static;audiovttforge\web_static")
# Conda-backed virtual environments keep these Python extension dependencies in
# the base interpreter's Library/bin, outside PyInstaller's usual DLL search.
# Include only the two concrete runtime libraries when present.
$basePrefixOutput = & $Python -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the selected Python runtime: $Python"
}
$basePrefix = ([string]$basePrefixOutput).Trim()
foreach ($runtimeDll in @("libmpdec-4.dll", "ffi.dll")) {
    $runtimePath = Join-Path $basePrefix "Library\bin\$runtimeDll"
    if (Test-Path -LiteralPath $runtimePath -PathType Leaf) {
        $extra += @("--add-binary", "$runtimePath;.")
    }
}
$name = "AudioVTTForge"
$entry = Join-Path $root "desktop_entry.py"
$windowArgs = @("--windowed")
$extra += @(
    "--collect-data", "webview",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.winforms"
)

New-Item -ItemType Directory -Force -Path $dist, $work, $staging | Out-Null
$common = @(
    "--noconfirm",
    "--name", $name,
    "--distpath", $staging,
    "--workpath", $work,
    "--specpath", $work,
    "--paths", $root
) + $extra

& $Python -m PyInstaller @common "--$Mode" @windowArgs $entry
if ($LASTEXITCODE -ne 0) {
    throw "EXE build failed during PyInstaller. Diagnostics are preserved at $runRoot."
}

if ($Mode -eq "onefile") {
    $built = Join-Path $staging "$name.exe"
    $targetPath = Join-Path $dist "$name.exe"
    $legacyPath = Join-Path $dist $name
} else {
    $built = Join-Path $staging $name
    $targetPath = Join-Path $dist $name
    $legacyPath = Join-Path $dist "$name.exe"
}
if (-not (Test-Path -LiteralPath $built)) {
    throw "PyInstaller completed without creating $built."
}

# pythonnet uses the .NET Framework loader on Windows.  On this machine the
# project folder belongs to the Internet zone, so .NET refuses to load the
# bundled Python.Runtime.dll without the executable's application config.
# Stage the config before replacement so an onedir build is always complete.
$runtimeConfig = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
"@
if ($Mode -eq "onefile") {
    $stagedConfig = "$built.config"
    $targetConfig = "$targetPath.config"
} else {
    $stagedConfig = Join-Path $built "$name.exe.config"
}
$runtimeConfig | Set-Content -LiteralPath $stagedConfig -Encoding UTF8

$replaced = $false
$lastError = $null
for ($attempt = 1; $attempt -le $ReplaceRetries; $attempt++) {
    $backup = Join-Path $runRoot ("previous-" + $attempt)
    $backedUp = $false
    try {
        if ($Mode -eq "onefile") {
            # The sidecar must be next to the EXE before the new EXE can start.
            Copy-Item -LiteralPath $stagedConfig -Destination $targetConfig -Force -ErrorAction Stop
        }
        if (Test-Path -LiteralPath $targetPath) {
            Move-ProjectItem $targetPath $backup
            $backedUp = $true
        }
        Move-ProjectItem $built $targetPath
        $replaced = $true
    } catch {
        $lastError = $_
        if ($backedUp -and -not (Test-Path -LiteralPath $targetPath) -and (Test-Path -LiteralPath $backup)) {
            Move-ProjectItem $backup $targetPath
        }
        if ($attempt -lt $ReplaceRetries) {
            Start-Sleep -Milliseconds ([Math]::Min(3000, 250 * $attempt))
        }
    }
    if ($replaced) {
        if ($backedUp) {
            try {
                Recycle-ProjectItem $backup
            } catch {
                Write-Warning "New build is ready; previous build remains at ${backup}: $($_.Exception.Message)"
            }
        }
        break
    }
}

if (-not $replaced) {
    throw "Could not replace $targetPath after $ReplaceRetries attempts. The new build is preserved at $built. Close the running app and retry. Last error: $($lastError.Exception.Message)"
}

if (Test-Path -LiteralPath $legacyPath) {
    try {
        Recycle-ProjectItem $legacyPath
    } catch {
        Write-Warning "New build is ready; older packaging format remains at ${legacyPath}: $($_.Exception.Message)"
    }
}
if ($Mode -eq "onedir" -and (Test-Path -LiteralPath "$legacyPath.config")) {
    try {
        Recycle-ProjectItem "$legacyPath.config"
    } catch {
        Write-Warning "Older EXE config remains at $legacyPath.config: $($_.Exception.Message)"
    }
}
if ($Mode -eq "onedir") {
    Write-Output "EXE: $(Join-Path $targetPath "$name.exe")"
} else {
    Write-Output "EXE: $targetPath"
}
Write-Output "Build diagnostics: $runRoot"
