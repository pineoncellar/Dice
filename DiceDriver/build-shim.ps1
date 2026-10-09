# Builds shim\dd_shim.cpp -> bin\dd_shim.dll (x64, MSVC, /MT, Release).
# Must use the same MSVC toolset + static CRT as the Dice DLL; keep vcvars64 and cl in ONE cmd process.
#   powershell -ExecutionPolicy Bypass -File build-shim.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bin = Join-Path $root "bin"
$obj = Join-Path $env:TEMP "dd-shim-obj"   # TEMP is on D:, keeps C: clean
New-Item -ItemType Directory -Force $bin, $obj | Out-Null

$vcvars = $env:DD_VCVARS64
if (-not $vcvars) {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vsPath = $null
    if (Test-Path $vswhere) {
        $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    }
    if (-not $vsPath) { $vsPath = "E:\VisualStdio\Community" }
    $vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
}
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found: $vcvars (set DD_VCVARS64)" }

$src = Join-Path $root "shim\dd_shim.cpp"
$out = Join-Path $bin "dd_shim.dll"
$cmd = "call `"$vcvars`" >nul && cl /nologo /std:c++17 /utf-8 /EHsc /O2 /MT /LD /Fo`"$obj\\`" /Fe`"$out`" `"$src`" /link /IMPLIB:`"$obj\dd_shim.lib`""
& $env:ComSpec /c $cmd
if ($LASTEXITCODE -ne 0) { throw "shim build failed ($LASTEXITCODE)" }
Write-Host "OK: $out"
