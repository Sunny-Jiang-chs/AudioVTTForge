param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name "AudioVTTForge" --distpath $dist --workpath $build (Join-Path $root "audio_vtt_to_mp4_gui.py")
if ($LASTEXITCODE -ne 0) { throw "EXE build failed" }
Write-Output "EXE: $(Join-Path $dist 'AudioVTTForge.exe')"
