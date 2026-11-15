# Turns on Windows Error Reporting local dumps for the driver's interpreter, so the next hard
# death inside the Dice DLL leaves a .dmp next to the logs. crash.log already holds the Python
# side of such a crash; the fault itself happens on a Dice-created thread that Python knows
# nothing about, and only a native dump has its stack.
#
#   powershell -ExecutionPolicy Bypass -File enable-wer-dumps.ps1              # C:\dumps, full dumps
#   powershell -ExecutionPolicy Bypass -File enable-wer-dumps.ps1 -DumpType 1  # mini: stacks only
#   powershell -ExecutionPolicy Bypass -File enable-wer-dumps.ps1 -Query       # what is set now
#   powershell -ExecutionPolicy Bypass -File enable-wer-dumps.ps1 -Disable     # back to Windows default
#
# Needs an elevated shell: the setting lives under HKLM. Nothing here touches the driver, and the
# setting survives reboots; turn it off with -Disable once the crash has been captured.
#
# After the next crash:
#   cdb -z C:\dumps\python.exe.*.dmp -c ".ecxr; kb; ~*k; q"
# .ecxr + kb is the faulting thread, ~*k is every thread including Dice's own - that is the one
# we are after. Without WinDbg, Application event log ID 1000 names the module and offset.

param(
    [string]$DumpFolder = "C:\dumps",
    [ValidateSet(1, 2)][int]$DumpType = 2,      # 1 = mini, 2 = full
    [ValidateRange(1, 50)][int]$DumpCount = 3,  # how many dumps WER keeps before overwriting
    [string]$ImageName = "python.exe",          # empty = every process on the machine
    [switch]$Disable,
    [switch]$Query
)

$ErrorActionPreference = "Stop"

$werRoot = "HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting"
$localDumps = Join-Path $werRoot "LocalDumps"
$target = $localDumps
if ($ImageName) { $target = Join-Path $localDumps $ImageName }

# The per-image key wins; the parent key is what a -ImageName "" call writes.
$keys = @($localDumps)
if ($ImageName) { $keys += $target }

function Show-Settings {
    $wer = Get-ItemProperty -Path $werRoot -ErrorAction SilentlyContinue
    if ($wer -and $wer.PSObject.Properties.Name -contains "Disabled" -and $wer.Disabled -eq 1) {
        Write-Warning "WER is switched off here (Disabled=1 under $werRoot); no dump will be written."
    }
    foreach ($key in $keys) {
        Write-Host ""
        Write-Host "[$key]"
        $props = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if (-not $props) {
            Write-Host "  (not set)"
            continue
        }
        foreach ($name in "DumpFolder", "DumpType", "DumpCount") {
            $value = $props.$name
            if ($null -eq $value) { $value = "(not set)" }
            Write-Host ("  {0,-10} = {1}" -f $name, $value)
        }
    }
}

if ($Query) {
    Show-Settings
    exit 0
}

if ($Disable) {
    # deepest first: removing the parent takes the child with it
    $remove = @()
    if ($target -ne $localDumps) { $remove += $target }
    $remove += $localDumps
    foreach ($key in $remove) {
        if (Test-Path $key) {
            Remove-Item -Path $key -Recurse -Force
            Write-Host "removed $key"
        }
        else {
            Write-Host "$key was not present"
        }
    }
    exit 0
}

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (right click > Run as administrator): the setting lives under HKLM."
}

if (-not (Test-Path $DumpFolder)) { New-Item -ItemType Directory -Force $DumpFolder | Out-Null }

# A full dump of the driver is a few hundred MB; say something before filling the disk with them.
$root = [IO.Path]::GetPathRoot($DumpFolder)
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($root.Substring(0, 2))'" -ErrorAction SilentlyContinue
if ($disk) {
    $freeGb = [math]::Round($disk.FreeSpace / 1GB, 1)
    Write-Host ("dump folder {0} ({1} GB free, keeping at most {2})" -f $DumpFolder, $freeGb, $DumpCount)
    if ($DumpType -eq 2 -and $freeGb -lt 5) {
        Write-Warning "Less than 5 GB free. Pass -DumpType 1 (thread stacks without the heap) or free some space first."
    }
}

if (-not (Test-Path $target)) { New-Item -Path $target -Force | Out-Null }
New-ItemProperty -Path $target -Name "DumpFolder" -PropertyType ExpandString -Value $DumpFolder -Force | Out-Null
New-ItemProperty -Path $target -Name "DumpType" -PropertyType DWord -Value $DumpType -Force | Out-Null
New-ItemProperty -Path $target -Name "DumpCount" -PropertyType DWord -Value $DumpCount -Force | Out-Null

Write-Host ""
Show-Settings
Write-Host ""
Write-Host "Armed. WER reads this when the fault happens, so the process running right now is"
Write-Host "covered too; the dump appears as $DumpFolder\$ImageName.<pid>.dmp and the exit code"
Write-Host "in logs\supervisor.log / logs\incidents\ tells which run it belongs to."
