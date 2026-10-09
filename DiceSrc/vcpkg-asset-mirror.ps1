# Asset fetcher for vcpkg (X_VCPKG_ASSET_SOURCES x-script).
#   Usage: vcpkg-asset-mirror.ps1 <url> <sha512> <dst>
#
# Why this exists: the local proxy (127.0.0.1:20808) crawls at ~30 KB/s for GitHub,
# while GitHub mirrors reach ~1.5 MB/s over a direct connection. vcpkg re-reads the
# Windows IE proxy settings and re-adds HTTP(S)_PROXY on its own, so downloads have to
# be routed here instead of relying on vcpkg's own downloader.
#
# Routes per asset, in order:
#   GitHub  -> ghfast.top, gh-proxy.com, then direct
#   other   -> local proxy, then direct (some hosts, e.g. SourceForge, reset direct
#              connections; others serve fine over the proxy)
# Exits 1 when every route fails, which lets vcpkg fall back to its own downloader.

# Arguments are parsed by hand: vcpkg appends "<url> <sha512> <dst>", but the sha512 is
# omitted for assets that have no hash, and an empty string passed through `-File` can be
# dropped entirely. With no param block everything lands in $args, so both forms bind
# correctly (a mid-list empty string shifts nothing).
$raw = @($args)
if ($raw.Count -ge 3) {
    $Url = $raw[0]; $Sha512 = $raw[1]; $Dst = $raw[2]
} elseif ($raw.Count -eq 2) {
    $Url = $raw[0]; $Sha512 = ""; $Dst = $raw[1]
} else {
    Write-Host "usage: vcpkg-asset-mirror.ps1 <url> [sha512] <dst>"
    exit 1
}

$ErrorActionPreference = "Continue"

# curl ships with Windows 10 1803+. Resolve it through %SystemRoot% / PATH rather than a
# hardcoded "C:\Windows" (Windows is not always installed on C:). If it is missing the
# script exits 1 below, which lets vcpkg fall back to its own downloader.
$curl = "curl.exe"
if ($env:SystemRoot) {
    $systemCurl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path $systemCurl) { $curl = $systemCurl }
}
if (-not (Get-Command $curl -ErrorAction SilentlyContinue)) {
    Write-Host "curl.exe not found; letting vcpkg download this asset itself"
    exit 1
}

$githubMirrors = @("https://ghfast.top/", "https://gh-proxy.com/")
$proxy = "http://127.0.0.1:20808"

$common = @("-sS", "-L", "-f", "--connect-timeout", "20")
# generous budget: keeps slow-but-progressing transfers alive, gives up on a stalled one
$patient = @("--max-time", "3600", "--speed-limit", "1024", "--speed-time", "120", "--retry", "2", "--retry-delay", "2")
# quick probe: used for last-resort routes so a dead host is abandoned fast
$probe = @("--max-time", "180", "--speed-limit", "8192", "--speed-time", "20", "--retry", "0")

$directMode = @("--noproxy", "*")
$proxyMode = @("--proxy", $proxy)

function Get-Asset([string]$source, [string[]]$mode, [string[]]$limits) {
    & $curl @common @limits @mode -o $Dst $source
    if ($LASTEXITCODE -ne 0) { return $false }
    if (-not (Test-Path $Dst)) { return $false }
    return (Get-Item $Dst).Length -gt 0
}

if ($Url -match '^https://(codeload\.)?github\.com/') {
    foreach ($mirror in $githubMirrors) {
        if (Get-Asset ($mirror + $Url) $directMode $patient) { exit 0 }
    }
    if (Get-Asset $Url $directMode $patient) { exit 0 }
    if (Get-Asset $Url $proxyMode $probe) { exit 0 }
    exit 1
}

if (Get-Asset $Url $proxyMode $patient) { exit 0 }
if (Get-Asset $Url $directMode $probe) { exit 0 }

exit 1
