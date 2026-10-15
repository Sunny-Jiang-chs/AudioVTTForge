[CmdletBinding()]
param(
    [string]$TempRoot = $env:TEMP,
    [switch]$WriteProbe,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$exitCode = 0

function Resolve-Directory([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'The temporary directory is not configured.' }
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
    if ($resolved.Provider.Name -ne 'FileSystem' -or -not (Test-Path -LiteralPath $resolved.ProviderPath -PathType Container)) {
        throw "Not a filesystem directory: $Path"
    }
    return [IO.Path]::GetFullPath($resolved.ProviderPath)
}

function Assert-ProbeBoundary([string]$Root, [string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd([char[]]'\/') + [IO.Path]::DirectorySeparatorChar
    if ($absolute.Length -le $prefix.Length -or -not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Probe path is outside the temporary directory: $absolute"
    }
    return $absolute
}

function Assert-NoReparseDirectory([string]$Path) {
    $directory = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while ($null -ne $directory) {
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Write probes cannot use a linked directory or ancestor: $($directory.FullName)"
        }
        $directory = $directory.Parent
    }
}

function Invoke-WriteProbe([string]$Root) {
    $resolvedRoot = Resolve-Directory $Root
    Assert-NoReparseDirectory $resolvedRoot
    $probePath = Assert-ProbeBoundary $resolvedRoot (Join-Path $resolvedRoot ('soundweaver-probe-' + [guid]::NewGuid().ToString('N')))
    $created = $false
    try {
        # No -Force: a name collision must fail without reusing any existing tree.
        New-Item -ItemType Directory -Path $probePath -ErrorAction Stop | Out-Null
        $created = $true
        Assert-NoReparseDirectory $probePath
        $probePath = Assert-ProbeBoundary $resolvedRoot (Resolve-Directory $probePath)
        $probeFile = Assert-ProbeBoundary $probePath (Join-Path $probePath 'write-probe.txt')
        $payload = 'Soundweaver temporary write probe.'
        $stream = [IO.File]::Open($probeFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Dispose()
        }
        if ([IO.File]::ReadAllText($probeFile) -ne $payload) { throw 'The probe read-back did not match.' }
        Write-Host "PASS write/read probe: $probePath"
    } finally {
        if ($created) {
            try {
                # Revalidate before recycling; never clean up any pre-existing tree.
                $resolvedRoot = Resolve-Directory $resolvedRoot
                Assert-NoReparseDirectory $probePath
                $safeProbe = Assert-ProbeBoundary $resolvedRoot (Resolve-Directory $probePath)
                Add-Type -AssemblyName Microsoft.VisualBasic
                [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
                    $safeProbe,
                    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
                    [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
                )
                Write-Host "Recycled probe directory: $safeProbe"
            } catch {
                throw "Probe cleanup was not confirmed; inspect the retained path $probePath. No permanent-delete fallback was attempted. $($_.Exception.Message)"
            }
        }
    }
}

try {
    Write-Host 'Soundweaver environment diagnostics'
    Write-Host "TEMP: $env:TEMP"
    Write-Host "TMP : $env:TMP"
    $resolvedTemp = Resolve-Directory $TempRoot
    Write-Host "Temporary directory exists: $resolvedTemp"

    $directoryExe = Join-Path $projectRoot 'dist\Soundweaver\Soundweaver.exe'
    $singleExe = Join-Path $projectRoot 'dist\Soundweaver.exe'
    if (Test-Path -LiteralPath $directoryExe -PathType Leaf) {
        Write-Host "PASS directory build: $directoryExe"
    } elseif (Test-Path -LiteralPath $singleExe -PathType Leaf) {
        Write-Host "PASS onefile build: $singleExe"
        if (-not (Test-Path -LiteralPath "$singleExe.config" -PathType Leaf)) {
            Write-Warning 'Soundweaver.exe.config is missing; keep it beside the onefile EXE.'
            $exitCode = 1
        }
    } else {
        Write-Warning 'No Soundweaver build found. Run build.ps1 to produce an application.'
        $exitCode = 1
    }

    if ($WriteProbe) {
        Invoke-WriteProbe $resolvedTemp
    } else {
        Write-Host 'READ ONLY: write access was NOT tested. Use -WriteProbe to opt in.'
    }
    Write-Host 'File presence does not verify WebView2, media tools, or application startup.'
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
}

if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
exit $exitCode
