#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$MountedDiscOrVolume,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$StagingDirectory
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'KylinOffline.Common.ps1')

$arguments = @(
    'stage'
    [System.IO.Path]::GetFullPath($MountedDiscOrVolume)
    [System.IO.Path]::GetFullPath($StagingDirectory)
)
$status = Invoke-KylinOfflineCli -Arguments $arguments

# An incomplete multi-disc set is expected and is not a PowerShell failure.
if ($status -eq 3) {
    Write-Host 'More volumes are required; insert and stage the next disc.'
    exit 0
}
exit $status
