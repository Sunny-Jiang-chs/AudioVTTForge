@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
echo ============================================
echo  AudioVTTForge 临时目录环境自检
echo ============================================
echo.

echo [1] TEMP / TMP 环境变量
echo     TEMP = %TEMP%
echo     TMP  = %TMP%
echo.

echo [2] 临时目录是否存在
if exist "%TEMP%\" (echo     OK   %TEMP% 存在) else (echo     FAIL %TEMP% 不存在  ^<== onefile 会启动失败)
if not "%TMP%"=="%TEMP%" (
    if exist "%TMP%\" (echo     OK   %TMP% 存在) else (echo     FAIL %TMP% 不存在)
)
echo.

echo [3] 临时目录是否可写
set "PROBE=%TEMP%\_avf_write_test.tmp"
>"%PROBE%" echo test 2>nul
if exist "%PROBE%" (
    echo     OK   可以创建文件
    del /f /q "%PROBE%" >nul 2>&1
) else (
    echo     FAIL 无法在 %TEMP% 创建文件  ^<== onefile 会启动失败
    echo          常见原因：杀毒软件/勒索防护（受控文件夹访问）拦截，或目录权限被改
)
echo.

echo [4] 临时目录能否创建子目录（_MEI 目录就在这里创建）
set "PROBEDIR=%TEMP%\_avf_dir_test"
mkdir "%PROBEDIR%" 2>nul
if exist "%PROBEDIR%\" (
    echo     OK   可以创建子目录
    rmdir /s /q "%PROBEDIR%" >nul 2>&1
) else (
    echo     FAIL 无法在 %TEMP% 创建子目录
)
echo.

echo [5] 残留的 _MEI 目录（异常退出会留下，可能干扰启动）
dir /b /ad "%TEMP%\_MEI*" 2>nul
if errorlevel 1 echo     无
echo.

echo [6] 磁盘剩余空间
dir /-c "%TEMP%" | findstr /i "可用字节 bytes free"
echo.

echo [7] EXE 是否存在
if exist "%~dp0dist\AudioVTTForge.exe" (
    echo     OK   %~dp0dist\AudioVTTForge.exe
) else (
    echo     FAIL 未找到 dist\AudioVTTForge.exe，请先运行 build.ps1
)
echo.
echo ============================================
echo  自检结束。若 [2] 或 [3] 为 FAIL，就是启动失败的直接原因。
echo ============================================
pause
