# Builds DiceDriver\shim\dd_shim.cpp -> <root>\output\dd_shim.dll (x64, MSVC, /MT, Release).
#
#   powershell -ExecutionPolicy Bypass -File build-shim.ps1
#   (or just run the bat at the workspace root: build-dicedriver.bat)
#
# Must use the same MSVC toolset + static CRT as the Dice DLL, so vcvars64 + cl stay in ONE
# cmd process. Only needed after shim\dd_shim.cpp changes; the Python part needs no build.

$ErrorActionPreference = "Stop"
$drv = $PSScriptRoot                                  # <root>\DiceDriver
$root = Split-Path $drv -Parent                       # workspace root
$out = Join-Path $root "output"
$obj = Join-Path $root "build\shim-obj"
New-Item -ItemType Directory -Force $out, $obj | Out-Null

$vcvars = $env:DD_VCVARS64
if (-not $vcvars) {
    # Discover VS through vswhere (no hardcoded install path). VS installer always puts it in
    # Program Files (x86) on 64-bit Windows, and on 32-bit there is no x64 toolchain anyway.
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vsPath = $null
    if (Test-Path $vswhere) {
        $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if (-not $vsPath) { $vsPath = & $vswhere -latest -products * -property installationPath }
    }
    if (-not $vsPath) {
        throw "Visual Studio (with the x64 C++ toolset) was not found. Set DD_VCVARS64 to the full path of vcvars64.bat, e.g. DD_VCVARS64='<VS>\VC\Auxiliary\Build\vcvars64.bat'."
    }
    $vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
}
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found: $vcvars (set DD_VCVARS64)" }

$src = Join-Path $drv "shim\dd_shim.cpp"
$dll = Join-Path $out "dd_shim.dll"
# NB: the temporary-directory argument must end in a doubled backslash. cl.exe parses
# \" as an escaped quote, so a single trailing backslash would swallow the quote and
# the path would be mangled (cl then reports "cannot open source file").
$cmd = "call `"$vcvars`" >nul && cl /nologo /std:c++17 /utf-8 /EHsc /O2 /MT /LD /Fo`"$obj\\`" /Fe`"$dll`" `"$src`" /link /IMPLIB:`"$obj\dd_shim.lib`""
& $env:ComSpec /c $cmd
if ($LASTEXITCODE -ne 0) { throw "shim build failed ($LASTEXITCODE)" }
Write-Host "OK: $dll"