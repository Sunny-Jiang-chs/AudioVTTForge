param(
    [ValidateSet("onedir", "onefile")][string]$Mode = "onedir",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$dist = Join-Path $root "dist"
$build = Join-Path $root "build"
$entry = Join-Path $root "audio_vtt_to_mp4_gui.py"

# 预检：onefile（单文件）依赖 %TEMP% 解包运行库，TEMP 不存在或不可写时 exe 会弹
# "Could not create temporary directory!" 且无法启动；onedir 不受影响。
$tmp = $env:TEMP
if ([string]::IsNullOrWhiteSpace($tmp) -or -not (Test-Path $tmp)) {
    Write-Warning "TEMP 无效：'$tmp'。onefile 构建的 exe 将无法启动，请先运行 diagnose-temp.bat"
} else {
    $probe = Join-Path $tmp ("_avf_probe_" + [guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Path $probe -ErrorAction Stop | Out-Null
        Remove-Item $probe -Recurse -Force
    } catch {
        Write-Warning "TEMP 不可写：$tmp。onefile 构建的 exe 将无法启动，请先运行 diagnose-temp.bat"
    }
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null

$common = @(
    "--noconfirm", "--clean",
    "--name", "AudioVTTForge",
    "--distpath", $dist,
    "--workpath", $build,
    # hooks/hook-tkinter.py：完整替代内置 hook，显式收集 Anaconda Library\bin 下的
    # tcl/tk DLL + Tcl/Tk 数据目录，否则打出的包启动即
    # ImportError: DLL load failed while importing _tkinter
    "--additional-hooks-dir", (Join-Path $root "hooks"),
    "--runtime-hook", (Join-Path $root "hooks\rthook-tkinter-dll.py")
)
if ($Mode -eq "onedir") {
    # 目录版不释放到 %TEMP%，启动更稳，也更容易被 SmartScreen 放行
    & $Python -m PyInstaller @common --onedir --windowed $entry
} else {
    & $Python -m PyInstaller @common --onefile --windowed $entry
}
if ($LASTEXITCODE -ne 0) { throw "EXE build failed" }

if ($Mode -eq "onedir") {
    Write-Output "EXE: $(Join-Path $dist 'AudioVTTForge\AudioVTTForge.exe')"
} else {
    Write-Output "EXE: $(Join-Path $dist 'AudioVTTForge.exe')"
}
