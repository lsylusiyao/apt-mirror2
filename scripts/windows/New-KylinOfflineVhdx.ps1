#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('\.vhdx$')]
    [string]$Path,

    [ValidateRange(64MB, 64TB)]
    [UInt64]$MaximumSizeBytes = 2TB,

    [ValidatePattern('^[D-Zd-z]$')]
    [string]$DriveLetter = 'R',

    [ValidatePattern('^[A-Za-z0-9_-]{1,15}$')]
    [string]$Label = 'KYLIN_OFFLINE'
)

$ErrorActionPreference = 'Stop'
$fullPath = [System.IO.Path]::GetFullPath($Path)
$parent = Split-Path -Parent $fullPath

if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Parent directory does not exist: $parent"
}
if (Test-Path -LiteralPath $fullPath) {
    throw "Refusing to overwrite an existing virtual disk: $fullPath"
}
if (Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue) {
    throw "Drive letter $DriveLetter`: is already in use"
}

# DiskPart's maximum value is expressed in MiB.  type=expandable creates a
# dynamically allocated VHDX: the host file starts small and grows as data is
# written, up to this ceiling.
$maximumMiB = [Math]::Ceiling($MaximumSizeBytes / 1MB)
$diskPartFile = New-TemporaryFile
try {
    @(
        "create vdisk file=`"$fullPath`" maximum=$maximumMiB type=expandable"
        "select vdisk file=`"$fullPath`""
        'attach vdisk'
        'convert gpt'
        'create partition primary'
        "format fs=exfat quick label=`"$Label`""
        "assign letter=$DriveLetter"
    ) | Set-Content -LiteralPath $diskPartFile -Encoding Ascii

    & diskpart.exe /s $diskPartFile
    if ($LASTEXITCODE -ne 0) {
        throw "DiskPart failed with exit code $LASTEXITCODE"
    }
}
finally {
    Remove-Item -LiteralPath $diskPartFile -Force -ErrorAction SilentlyContinue
}

$disk = Get-DiskImage -ImagePath $fullPath | Get-Disk
Write-Host "Created dynamic VHDX: $fullPath"
Write-Host "Mounted as: $DriveLetter`: (disk $($disk.Number), maximum $maximumMiB MiB)"
Write-Host 'Use Dismount-DiskImage -ImagePath <path> before unplugging the physical disk.'
