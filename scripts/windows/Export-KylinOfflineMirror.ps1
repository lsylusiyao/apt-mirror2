#Requires -Version 5.1

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MediaRoot,

    [string]$WslDistribution = 'Ubuntu',
    [string]$MirrorConfig = '/etc/apt/mirror-kylin.list',
    [string]$MirrorRoot = '/var/spool/apt-mirror/mirror',
    [string]$StateDirectory = '/var/lib/apt-mirror-offline',
    [string]$VolumeSize = '0',
    [switch]$SkipOnlineSync,
    [switch]$RehashSource
)

$ErrorActionPreference = 'Stop'

function Convert-ToWslPath([string]$InputPath) {
    if ($InputPath.StartsWith('/')) {
        return $InputPath
    }
    $converted = & wsl.exe -d $WslDistribution -- wslpath -a -u $InputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to convert Windows path for WSL: $InputPath"
    }
    return $converted.Trim()
}

if (-not (Test-Path -LiteralPath $MediaRoot -PathType Container)) {
    throw "Media root is not mounted: $MediaRoot"
}

$configWsl = Convert-ToWslPath $MirrorConfig
$mediaFull = (Resolve-Path -LiteralPath $MediaRoot).Path
$feedbackWindows = Join-Path $mediaFull 'feedback'
$outgoingWindows = Join-Path $mediaFull 'outgoing'
New-Item -ItemType Directory -Force -Path $feedbackWindows, $outgoingWindows | Out-Null
$feedbackWsl = Convert-ToWslPath $feedbackWindows

if (-not $SkipOnlineSync) {
    Write-Host 'Synchronizing archive.kylinos.cn in WSL...'
    & wsl.exe -d $WslDistribution -u root -- apt-mirror $configWsl
    if ($LASTEXITCODE -ne 0) {
        throw "apt-mirror failed with exit code $LASTEXITCODE; no offline bundle was created"
    }
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$bundleWindows = Join-Path $outgoingWindows "bundle-$stamp"
$bundleWsl = Convert-ToWslPath $bundleWindows
$offlineArguments = @(
    '-d', $WslDistribution, '-u', 'root', '--',
    'apt-mirror-offline', 'export', $MirrorRoot, $bundleWsl,
    '--state-dir', $StateDirectory,
    '--feedback-dir', $feedbackWsl,
    '--volume-size', $VolumeSize
)
if ($RehashSource) {
    $offlineArguments += '--rehash-source'
}

Write-Host 'Building verified incremental bundle...'
& wsl.exe @offlineArguments
if ($LASTEXITCODE -ne 0) {
    throw "apt-mirror-offline failed with exit code $LASTEXITCODE"
}
& wsl.exe -d $WslDistribution -u root -- sync

Write-Host "Bundle ready: $bundleWindows"
if ($VolumeSize -ne '0') {
    Write-Host 'For optical media, burn the CONTENTS of each volumes\volume-NNNN directory to one disc.'
}
Write-Host 'Dismount/eject the filesystem cleanly before moving the physical medium.'
