#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$MediaRoot,

    [string]$WslDistribution = 'Ubuntu',
    [string]$MirrorConfig = '/etc/apt/mirror-kylin.list',
    [string]$MirrorRoot = '/var/spool/apt-mirror/mirror',
    [string]$StateDirectory = '/var/lib/apt-mirror-offline',
    [string]$VolumeSize = '0',
    [switch]$SkipOnlineSync,
    [switch]$RehashSource,
    [switch]$HashOnly
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

if (-not $HashOnly -and [string]::IsNullOrWhiteSpace($MediaRoot)) {
    throw 'MediaRoot is required unless HashOnly is specified.'
}
if (-not $HashOnly -and -not (Test-Path -LiteralPath $MediaRoot -PathType Container)) {
    throw "Media root is not mounted: $MediaRoot"
}

$configWsl = Convert-ToWslPath $MirrorConfig
$mirrorRootWsl = Convert-ToWslPath $MirrorRoot
$stateDirectoryWsl = Convert-ToWslPath $StateDirectory
if (-not $HashOnly) {
    $mediaFull = (Resolve-Path -LiteralPath $MediaRoot).Path
    $feedbackWindows = Join-Path $mediaFull 'feedback'
    $outgoingWindows = Join-Path $mediaFull 'outgoing'
    New-Item -ItemType Directory -Force -Path $feedbackWindows, $outgoingWindows | Out-Null
    $feedbackWsl = Convert-ToWslPath $feedbackWindows
}

if (-not $SkipOnlineSync) {
    $syncSucceeded = $false
    try {
        & wsl.exe -d $WslDistribution -u root -- python3 -c `
            'from apt_mirror.download.downloader import Downloader; assert Downloader.PARTIAL_DIRECTORY == ".apt-mirror2-partial"'
        if ($LASTEXITCODE -ne 0) {
            throw 'The resume-enabled version of this project is not installed for WSL root Python 3.'
        }
        Write-Host 'Synchronizing archive.kylinos.cn in WSL (an interrupted rerun resumes HTTP partials)...'
        & wsl.exe -d $WslDistribution -u root -- python3 -m apt_mirror $configWsl
        $syncExitCode = $LASTEXITCODE
        if ($syncExitCode -ne 0) {
            throw "apt-mirror failed with exit code $syncExitCode; no offline bundle was created"
        }
        $syncSucceeded = $true
    }
    finally {
        if (-not $syncSucceeded) {
            & wsl.exe -d $WslDistribution -u root -- sync
            Write-Warning 'Online synchronization did not finish. Completed files and HTTP partial downloads were retained.'
            Write-Warning 'Rerun this script with the same MirrorConfig and MirrorRoot to continue; do not use SkipOnlineSync yet.'
        }
    }
}

if ($HashOnly) {
    $hashArguments = @(
        '-d', $WslDistribution, '-u', 'root', '--',
        'python3', '-m', 'apt_mirror.offline', 'hash', $mirrorRootWsl,
        '--state-dir', $stateDirectoryWsl
    )
    if ($RehashSource) {
        $hashArguments += '--rehash-source'
    }

    Write-Host 'Hashing mirror without creating outgoing data...'
    & wsl.exe @hashArguments
    if ($LASTEXITCODE -ne 0) {
        throw "apt-mirror-offline hash failed with exit code $LASTEXITCODE"
    }
    & wsl.exe -d $WslDistribution -u root -- sync
    Write-Host "Hash cache updated in $StateDirectory (hash-cache.json)"
    return
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$bundleWindows = Join-Path $outgoingWindows "bundle-$stamp"
$bundleWsl = Convert-ToWslPath $bundleWindows
$offlineArguments = @(
    '-d', $WslDistribution, '-u', 'root', '--',
    'python3', '-m', 'apt_mirror.offline', 'export', $mirrorRootWsl, $bundleWsl,
    '--state-dir', $stateDirectoryWsl,
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
