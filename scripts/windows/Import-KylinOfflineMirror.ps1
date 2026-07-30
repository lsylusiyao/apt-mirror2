#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Bundle,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$MirrorRoot,

    [Parameter(Mandatory = $true, Position = 2)]
    [string]$FeedbackDirectory,

    [Parameter(Position = 3)]
    [ValidateSet('prompt', 'report', 'apply')]
    [string]$DeletePolicy = 'prompt',

    [switch]$AllowLargeDeletes
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'KylinOffline.Common.ps1')

$arguments = @(
    'import'
    [System.IO.Path]::GetFullPath($Bundle)
    [System.IO.Path]::GetFullPath($MirrorRoot)
    '--feedback-dir'
    [System.IO.Path]::GetFullPath($FeedbackDirectory)
    '--delete-policy'
    $DeletePolicy
)
if ($AllowLargeDeletes) {
    $arguments += '--allow-large-deletes'
}

$status = Invoke-KylinOfflineCli -Arguments $arguments
if ($status -eq 3) {
    $pendingFile = Join-Path $FeedbackDirectory 'deletions-pending.json'
    Write-Warning "Deletion review is pending. Review $pendingFile and rerun with -DeletePolicy apply."
}
exit $status
