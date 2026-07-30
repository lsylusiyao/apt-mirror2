#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$MirrorRoot,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$FeedbackDirectory
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'KylinOffline.Common.ps1')

$arguments = @(
    'verify'
    [System.IO.Path]::GetFullPath($MirrorRoot)
    '--feedback-dir'
    [System.IO.Path]::GetFullPath($FeedbackDirectory)
)
exit (Invoke-KylinOfflineCli -Arguments $arguments)
