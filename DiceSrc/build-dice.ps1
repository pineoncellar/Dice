# Dice! (DiceSrc) build script -- x64 only, workspace-layout aware.
#
#   Configure + build : powershell -ExecutionPolicy Bypass -File DiceSrc\build-dice.ps1
#   Build only        : ... -File DiceSrc\build-dice.ps1 -BuildOnly
#   (or just run the bat at the workspace root: build-dicesrc.bat)
#
# Re-running after an interruption (reboot, blue screen) resumes from vcpkg's binary cache,
# so re-run WITHOUT -BuildOnly: the CMake configure step is what builds the dependencies.
#
# Layout it assumes:
#   <root>\DiceSrc\    this script + the source tree + vcpkg (submodule)
#   <root>\build\      every intermediate product (CMake cache, objects, vcpkg cache/logs)
#   <root>\output\     the only thing you should need: w4123.Dice.windows.amd64.dll
#
# NOTE: x64 only, on purpose. The official DiceDriver builds are 32-bit and cannot load this
# DLL, so the triplet/arch/suffix are pinned here instead of being parameters.

param(
    [switch]$BuildOnly,
    [string]$BuildDir = ""
)

$ErrorActionPreference = "Continue"

$Triplet = "x64-windows-static"
$Arch = "x64"
$DllSuffix = ".windows.amd64"

$src = $PSScriptRoot                                  # <root>\DiceSrc
$root = Split-Path $src -Parent                       # workspace root
$build = if ($BuildDir) { $BuildDir } else { Join-Path $root "build\dice" }
$cache = Join-Path $root "build\vcpkg-cache"
$out = Join-Path $root "output"
$logDir = Join-Path $root "build\logs"
$log = Join-Path $logDir ("build-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

# The local proxy (127.0.0.1:20808) throttles large downloads to ~30 KB/s, while GitHub
# mirrors reach ~1.5 MB/s over a direct connection. vcpkg re-reads the Windows IE proxy
# settings and re-adds HTTP(S)_PROXY itself, so the only way to keep downloads off the
# proxy is to fetch them through an x-script asset source.
# The system PowerShell, resolved from %SystemRoot% instead of a hardcoded "C:\Windows"
# (Windows is not always installed on C:).
$psExe = "powershell.exe"
if ($env:SystemRoot) {
    $systemPs = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path $systemPs) { $psExe = $systemPs }
}

Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY, Env:http_proxy, Env:https_proxy -ErrorAction SilentlyContinue
$env:NO_PROXY = "*"
# NB: the x-script command is a plain command line, so a workspace path containing spaces
# would need its script path quoted here.
$env:X_VCPKG_ASSET_SOURCES = 'clear;x-script,' + $psExe + ' -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (Join-Path $src "vcpkg-asset-mirror.ps1") + ' {url} {sha512} {dst}'

# Keep vcpkg's binary cache off C:; vcpkg's own buildtrees/downloads follow the submodule
# (CMakeLists.txt pins the toolchain to ${CMAKE_CURRENT_SOURCE_DIR}/vcpkg).
$env:VCPKG_DEFAULT_BINARY_CACHE = $cache
$env:VCPKG_BINARY_SOURCES = "clear;files,$cache,readwrite"
$env:VCPKG_DOWNLOADS = Join-Path $src "vcpkg\downloads"
# 20 logical cores but only ~16 GB RAM: cap parallel port builds to avoid paging thrash
$env:VCPKG_MAX_CONCURRENCY = "6"

New-Item -ItemType Directory -Force -Path $cache, $build, $out, $logDir | Out-Null

function Invoke-Logged([string]$label, [scriptblock]$action) {
    $stamp = Get-Date -Format "HH:mm:ss"
    "===== [$stamp] $label =====" | Out-File -FilePath $log -Encoding utf8 -Append
    & $action 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append
    $code = $LASTEXITCODE
    "===== [$stamp] $label exit=$code =====" | Out-File -FilePath $log -Encoding utf8 -Append
    if ($code -ne 0) {
        Write-Host "$label FAILED (exit $code). Last lines of $log :"
        Get-Content $log -Tail 60
        exit $code
    }
}

Write-Host "source   : $src"
Write-Host "triplet  : $Triplet  arch: $Arch  suffix: $DllSuffix"
Write-Host "build dir: $build"
Write-Host "output   : $out"
Write-Host "log      : $log"

if (-not $BuildOnly) {
    Write-Host "=== configure: vcpkg builds every dependency, slow on first run ==="
    Invoke-Logged "configure" {
        # NB: "-DNAME=$value" must be a quoted string / array element, a bare
        # "-DNAME=$value" token is not expanded by PowerShell.
        $cmakeArgs = @(
            "-S", $src,
            "-B", $build,
            "-A", $Arch,
            "-DVCPKG_TARGET_TRIPLET=$Triplet",
            "-DDLL_SUFFIX=$DllSuffix",
            "-DVCPKG_INSTALL_OPTIONS=--keep-going",
            # land the DLL straight in <root>\output, not in <build>\Release
            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE=$out",
            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELWITHDEBINFO=$out",
            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG=$out"
        )
        & cmake @cmakeArgs
    }
}

Write-Host "=== build: w4123.Dice ==="
Invoke-Logged "build" {
    cmake --build $build --config Release --parallel 8
}

Write-Host "=== done ==="
Get-ChildItem "$out\w4123.Dice*.dll" -ErrorAction SilentlyContinue | Select-Object FullName, Length