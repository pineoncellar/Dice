# Assembles a self-contained DiceDriver bundle for a machine that has no development
# environment at all: no system Python, no venv, no MSVC, no VC++ redistributable.
#
#   powershell -ExecutionPolicy Bypass -File build-portable.ps1
#   powershell -ExecutionPolicy Bypass -File build-portable.ps1 -PythonZip D:\dl\python-3.11.9-embed-amd64.zip -Zip
#
# It packages the normal build products, so produce those first:
#   build-dicesrc.bat     -> <root>\output\w4123.Dice*.dll
#   build-dicedriver.bat  -> <root>\output\dd_shim.dll  and  DiceDriver\.venv (the source of websockets)
#
# Layout it produces (default -OutDir <root>\portable\Dice):
#   runtime\                        embeddable CPython + vendored websockets
#   DiceDriver\dicedriver\          the host package
#   Diceki\                         dd_shim.dll, the Dice DLL and dicedriver.toml, side by side
#   data\                           Dice's data dir; Dice<QQ>\ is created on first start
#   start-dicedriver-portable.bat   launcher that runs on runtime\python.exe
#   README-portable.txt             notes for whoever runs it on the target machine
#
# The whole <OutDir> is relocatable: copy it to any Windows x64 machine and run it there.
#
# The config is bundled into Diceki\ beside the DLLs it points at, so nothing has to be edited
# on the target machine. That move needs no source change -- config.py resolves relative paths
# against the config file's own directory, and the self-restart passes an absolute --config.
# Why an embeddable runtime rather than a plain copy of .venv: a venv is not relocatable
# (pyvenv.cfg records the absolute path of the interpreter it was created from), while the
# embeddable package is just a folder of files.
#
# NOTE: this file must stay UTF-8 *with BOM*. Windows PowerShell 5.1 parses a BOM-less
# script as ANSI, which would corrupt the Chinese README text embedded further down.

param(
    [string]$OutDir = "",
    [string]$PythonZip = "",
    [string]$PythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
    [switch]$Zip,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot                                  # workspace root
$drv = Join-Path $root "DiceDriver"
$artifacts = Join-Path $root "output"
$zipCache = Join-Path $root "build\portable"

if (-not $OutDir) { $OutDir = Join-Path $root "portable\Dice" }
$OutDir = [System.IO.Path]::GetFullPath($OutDir)

function Require-Leaf([string]$Path, [string]$Hint) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "missing: $Path`n  -> $Hint"
    }
}

# Copies the *contents* of a directory, skipping any path segment whose name is listed.
# Robocopy is not used so the exclusion list stays portable and readable.
function Copy-Tree([string]$Source, [string]$Destination, [string[]]$ExcludeDirNames) {
    $src = (Resolve-Path -LiteralPath $Source).Path
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $src -Recurse -Force -File | ForEach-Object {
        $rel = $_.FullName.Substring($src.Length).TrimStart('\')
        $skip = $false
        foreach ($name in $ExcludeDirNames) {
            if ($rel -like "$name\*" -or $rel -like "*\$name\*") { $skip = $true; break }
        }
        if (-not $skip) {
            $target = Join-Path $Destination $rel
            New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

# curl ships with Windows 10 1803+. It is preferred over Invoke-WebRequest because of the
# byte-progress/timeout options; the system copy is resolved through %SystemRoot% rather
# than a hardcoded C:\Windows.
function Get-File([string]$Url, [string]$Destination) {
    $curl = "curl.exe"
    if ($env:SystemRoot) {
        $systemCurl = Join-Path $env:SystemRoot "System32\curl.exe"
        if (Test-Path $systemCurl) { $curl = $systemCurl }
    }
    if (-not (Get-Command $curl -ErrorAction SilentlyContinue)) {
        throw "curl.exe not found; download`n  $Url`nand pass it as -PythonZip"
    }

    # generous budget: keeps slow-but-progressing transfers alive, gives up on a stalled one
    $common = @("-sS", "-L", "-f", "--connect-timeout", "20", "--retry", "2", "--retry-delay", "2",
                "--max-time", "3600", "--speed-limit", "1024", "--speed-time", "120")

    # system proxy first (that is what the plain curl probe uses), then a direct attempt
    foreach ($mode in @(@(), @("--noproxy", "*"))) {
        & $curl @common @mode -o $Destination $Url
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $Destination) -and (Get-Item -LiteralPath $Destination).Length -gt 0) {
            return
        }
        Remove-Item -LiteralPath $Destination -ErrorAction SilentlyContinue
    }
    throw "download failed: $Url`n  -> download it in a browser and pass it as -PythonZip"
}

# ------------------------------------------------------------------ preflight

Write-Host "workspace : $root"
Write-Host "output dir: $OutDir"

$shim = Join-Path $artifacts "dd_shim.dll"
Require-Leaf $shim "run build-dicedriver.bat first"

$diceDlls = @(Get-ChildItem -LiteralPath $artifacts -Filter "w4123.Dice*.dll" -File -ErrorAction SilentlyContinue)
if ($diceDlls.Count -eq 0) {
    throw "missing: $artifacts\w4123.Dice*.dll`n  -> run build-dicesrc.bat first"
}

$pkg = Join-Path $drv "dicedriver"
Require-Leaf (Join-Path $pkg "__main__.py") "the DiceDriver source tree looks incomplete"

$toml = Join-Path $drv "dicedriver.toml"
Require-Leaf $toml "copy dicedriver.example.toml to dicedriver.toml and edit it (it is gitignored)"

$ws = Join-Path $drv ".venv\Lib\site-packages\websockets"
Require-Leaf (Join-Path $ws "__init__.py") "run build-dicedriver.bat first (it creates .venv and installs websockets)"

# ------------------------------------------------------------------ python runtime

if ($PythonZip) {
    $zipPath = [System.IO.Path]::GetFullPath($PythonZip)
    Require-Leaf $zipPath "-PythonZip must point to python-3.11.x-embed-amd64.zip"
}
else {
    New-Item -ItemType Directory -Force -Path $zipCache | Out-Null
    $zipPath = Join-Path $zipCache (Split-Path $PythonUrl -Leaf)
    if (Test-Path -LiteralPath $zipPath) {
        Write-Host "runtime   : reusing cached $zipPath"
    }
    else {
        Write-Host "runtime   : downloading $PythonUrl"
        Get-File $PythonUrl $zipPath
    }
}

# Validate before extracting: a truncated download or a proxy error page would otherwise
# surface much later as a confusing "python.exe is not a valid application".
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    $entries = @($archive.Entries | ForEach-Object { $_.FullName })
}
finally {
    $archive.Dispose()
}
$pthEntries = @($entries | Where-Object { $_ -like "python3*._pth" })
$stdlibEntries = @($entries | Where-Object { $_ -like "python3*.zip" })
if (-not ($entries -contains "python.exe") -or $pthEntries.Count -eq 0 -or $stdlibEntries.Count -eq 0) {
    throw "$zipPath is not a CPython embeddable package (expected python.exe, python3*._pth and python3*.zip inside)"
}

if (Test-Path -LiteralPath $OutDir) {
    if (-not $Force) {
        throw "$OutDir already exists. Pass -Force to replace it, or pick another -OutDir."
    }
    Write-Host "cleaning   : $OutDir"
    Remove-Item -LiteralPath $OutDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$runtime = Join-Path $OutDir "runtime"
Write-Host "extracting: $zipPath -> $runtime"
[System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $runtime)

# ------------------------------------------------------------------ assemble

Write-Host "vendoring : websockets (from DiceDriver\.venv)"
Copy-Tree $ws (Join-Path $runtime "Lib\site-packages\websockets") @("__pycache__")

Write-Host "copying   : dicedriver package"
Copy-Tree $pkg (Join-Path $OutDir "DiceDriver\dicedriver") @("__pycache__")

Write-Host "copying   : built DLLs and config"
$diceki = Join-Path $OutDir "Diceki"
New-Item -ItemType Directory -Force -Path $diceki | Out-Null
Copy-Item -LiteralPath $shim -Destination $diceki -Force
foreach ($dll in $diceDlls) { Copy-Item -LiteralPath $dll.FullName -Destination $diceki -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $OutDir "data") | Out-Null

# The config now lives where the DLLs are, so its ../output paths (the dev-tree layout) no
# longer resolve: rewriting that one prefix is the whole adjustment. root_dir/state_dir keep
# working because ../data still lands on <OutDir>\data.
try {
    $cfgText = [System.IO.File]::ReadAllText($toml, (New-Object System.Text.UTF8Encoding($false, $true)))
}
catch {
    throw "$toml is not valid UTF-8; re-save it as UTF-8 and run this script again"
}
$cfgText = $cfgText -replace '\.\./output', '.'
[System.IO.File]::WriteAllText((Join-Path $diceki "dicedriver.toml"), $cfgText, (New-Object System.Text.UTF8Encoding($false)))

# A bundle is only self-contained if the config resolves its DLLs inside it. Absolute paths in
# a hand-edited config (or a config pointing at some other build) would silently break on the
# target machine, so say so now.
foreach ($key in @("dice_dll", "shim_dll")) {
    if ($cfgText -match ('(?m)^\s*' + $key + '\s*=\s*"([^"]*)"')) {
        $value = $Matches[1]
        $resolved = if ([System.IO.Path]::IsPathRooted($value)) { $value } else { [System.IO.Path]::GetFullPath((Join-Path $diceki $value)) }
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            Write-Warning "$key in the bundled config resolves outside this bundle ($resolved)"
        }
    }
}

# The ._pth file is what makes the bundle self-contained. Entries are resolved against the
# directory that holds python.exe, so ../DiceDriver keeps working after any move. An
# existing ._pth also puts the interpreter into isolated path mode, which means PYTHONPATH
# and PYTHONHOME have no effect -- everything has to be listed here.
$pthPath = Join-Path $runtime $pthEntries[0]
@(
    $stdlibEntries[0]
    "."
    "Lib\site-packages"
    "..\DiceDriver"
) | Set-Content -LiteralPath $pthPath -Encoding ASCII

# ------------------------------------------------------------------ launcher and notes

$launcherName = "start-dicedriver-portable.bat"
$launcher = @'
@echo off
rem Portable launcher: runs DiceDriver on the bundled embeddable Python.
rem No system Python, no venv and no development tools are required.
rem Stop it with Ctrl+C in this window.
setlocal
title DiceDriver (portable)
cd /d "%~dp0DiceDriver"

if not exist "%~dp0runtime\python.exe" (
    echo [ERROR] runtime\python.exe is missing from this bundle.
    pause
    exit /b 1
)
if not exist "%~dp0Diceki\dicedriver.toml" (
    echo [ERROR] Diceki\dicedriver.toml is missing from this bundle.
    pause
    exit /b 1
)

echo Starting DiceDriver ... press Ctrl+C to stop.
echo   python : %~dp0runtime\python.exe
echo   config : %~dp0Diceki\dicedriver.toml
echo   logs   : %~dp0Diceki\logs
echo   data   : %~dp0data
"%~dp0runtime\python.exe" -m dicedriver --config "%~dp0Diceki\dicedriver.toml" %*

echo.
echo DiceDriver exited with errorlevel %errorlevel%.
pause
exit /b %errorlevel%
'@
$launcher | Set-Content -LiteralPath (Join-Path $OutDir $launcherName) -Encoding ASCII

$readme = @'
DiceDriver 便携包
=================

这个目录可以整体拷到任何 Windows x64 电脑上运行，目标机不需要装 Python、venv、
Visual Studio、编译器或 VC++ 运行库。里面装的是一份 embeddable CPython 3.11 和一个
本地随包携带的 websockets 副本。

目录说明
--------
  runtime\                       随包的 Python（唯一的解释器来源）
  DiceDriver\dicedriver\         宿主程序本体
  Diceki\                        编译产物与配置：dd_shim.dll、Dice DLL、dicedriver.toml
  Diceki\logs\                   运行日志（首次启动自动创建）
  data\                          Dice 的数据目录，首次启动时会建出 Dice<骰娘QQ>\
  start-dicedriver-portable.bat  启动脚本

运行
----
  1) 把本目录整个拷到目标机（建议 D:\Dice 这类纯 ASCII 路径）
  2) 双击 start-dicedriver-portable.bat
  3) 想先自检再启动，可以执行：
       start-dicedriver-portable.bat --check
     它会校验配置与 DLL 路径并打印一行 OK，不会连接也不会启动 Dice

配置
----
  配置就是 Diceki\dicedriver.toml，紧挨着它要加载的两份 DLL。要改 OneBot 地址、token、
  账号、日志级别等，编辑它即可；它里面的相对路径都以自身所在的 Diceki\ 为基准
  （./ 是 DLL，../data 是骰娘数据目录），所以整个便携目录随便挪，不用改任何路径。

目标机需要什么
--------------
  - 64 位 Windows（x64 的 DLL 在 32 位系统上装不了）
  - 一个 OneBot 11 实现（NapCat / LLOneBot / Lagrange 等），驱动只是 Dice DLL 的宿主
  - 反向 WS 模式下要在防火墙上放通 dicedriver.toml 里配的监听端口

注意
----
  - 路径里不要出现非 ANSI 字符（例如把包放进口语化命名的文件夹）。Dice 用 ANSI 代码页
    解析 root_dir，中文用户目录在英文系统上会解析失败。建议就放在 D:\Dice 这类纯 ASCII 路径。
  - 想带上原来的骰娘数据，把旧的 data\Dice<骰娘QQ>\ 整个拷进本目录的 data\ 即可。
  - 本包的 Diceki\ 只是驱动侧存放编译产物与配置的目录；Dice 自身还会在 data\ 下用到它自己的
    Diceki\（存放骰娘的 Python 环境），两者互不相干。
  - 这个包不含 Dice 自身的 Python 脚本支持（Diceki）；Dice 的内建功能不受影响。
  - 重新生成这个包请回到开发机跑 build-portable.ps1。
'@
$readme | Set-Content -LiteralPath (Join-Path $OutDir "README-portable.txt") -Encoding UTF8

# ------------------------------------------------------------------ optional archive

$zipOut = ""
if ($Zip) {
    $parent = Split-Path $OutDir -Parent
    $zipOut = Join-Path $parent ((Split-Path $OutDir -Leaf) + "-portable.zip")
    if (Test-Path -LiteralPath $zipOut) { Remove-Item -LiteralPath $zipOut -Force }
    Write-Host "archiving : $zipOut"
    Compress-Archive -Path $OutDir -DestinationPath $zipOut -CompressionLevel Optimal
}

# ------------------------------------------------------------------ report

$total = (Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object Length -Sum).Sum
$diceNames = ($diceDlls | ForEach-Object { $_.Name }) -join ", "
Write-Host ""
Write-Host "=== portable bundle ready ==="
Write-Host ("  dir      : {0}" -f $OutDir)
Write-Host ("  size     : {0:N1} MB" -f ($total / 1MB))
Write-Host ("  python   : {0}" -f (Split-Path $zipPath -Leaf))
Write-Host ("  dice dll : {0}" -f $diceNames)
Write-Host ("  config   : {0}" -f (Join-Path $diceki "dicedriver.toml"))
if ($zipOut) { Write-Host ("  archive  : {0}" -f $zipOut) }
Write-Host ""
Write-Host "Copy this directory to the target machine and run start-dicedriver-portable.bat"
Write-Host "Verify it first with: start-dicedriver-portable.bat --check"
