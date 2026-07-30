#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('\.(vhd|vhdx|vmdk)$')]
    [string]$ImagePath,

    [ValidatePattern('^[D-Zd-z]$')]
    [string]$DriveLetter = 'R',

    [ValidateRange(1, 128)]
    [int]$VmdkVolume = 1,

    [string]$VmwareMountPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Command
)

$ErrorActionPreference = 'Stop'
$fullImagePath = [System.IO.Path]::GetFullPath($ImagePath)
if (-not (Test-Path -LiteralPath $fullImagePath -PathType Leaf)) {
    throw "Virtual disk does not exist: $fullImagePath"
}

function Find-VmwareMount {
    if ($VmwareMountPath) {
        $candidate = [System.IO.Path]::GetFullPath($VmwareMountPath)
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "vmware-mount.exe does not exist: $candidate"
        }
        return $candidate
    }

    $commandInfo = Get-Command 'vmware-mount.exe' -ErrorAction SilentlyContinue
    if ($null -ne $commandInfo) {
        return $commandInfo.Source
    }

    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} 'VMware\VMware Virtual Disk Development Kit\bin\vmware-mount.exe'
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles 'VMware\VMware Workstation\vmware-mount.exe'
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw 'A real VMDK requires VMware DiskMount (vmware-mount.exe); Windows cannot mount VMDK natively.'
}

function Invoke-RequestedCommand([string]$MediaRoot) {
    $expandedCommand = @(
        foreach ($item in $Command) {
            $item.Replace('{MEDIA_ROOT}', $MediaRoot)
        }
    )
    $executable = $expandedCommand[0]
    $childArguments = @()
    if ($expandedCommand.Count -gt 1) {
        $childArguments = $expandedCommand[1..($expandedCommand.Count - 1)]
    }

    $oldMediaRoot = [Environment]::GetEnvironmentVariable(
        'KYLIN_OFFLINE_MEDIA_ROOT',
        [EnvironmentVariableTarget]::Process
    )
    try {
        [Environment]::SetEnvironmentVariable(
            'KYLIN_OFFLINE_MEDIA_ROOT',
            $MediaRoot,
            [EnvironmentVariableTarget]::Process
        )
        & $executable @childArguments | Out-Host
        if ($null -eq $LASTEXITCODE) {
            return 0
        }
        return $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable(
            'KYLIN_OFFLINE_MEDIA_ROOT',
            $oldMediaRoot,
            [EnvironmentVariableTarget]::Process
        )
    }
}

$extension = [System.IO.Path]::GetExtension($fullImagePath).ToLowerInvariant()
$nativeImageMounted = $false
$vmdkMounted = $false
$vmwareMount = $null
$actualDriveLetter = $null
$commandStatus = 1

try {
    if ($extension -eq '.vmdk') {
        if (Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue) {
            throw "Drive letter $DriveLetter`: is already in use"
        }
        $vmwareMount = Find-VmwareMount
        & $vmwareMount "$DriveLetter`:" $fullImagePath "/v:$VmdkVolume" '/m:w' | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "vmware-mount.exe failed with exit code $LASTEXITCODE"
        }
        $vmdkMounted = $true
        $actualDriveLetter = $DriveLetter.ToUpperInvariant()
    }
    else {
        $existingImage = Get-DiskImage -ImagePath $fullImagePath -ErrorAction SilentlyContinue
        if ($null -ne $existingImage -and $existingImage.Attached) {
            throw "Virtual disk is already attached: $fullImagePath"
        }
        $mountedImage = Mount-DiskImage -ImagePath $fullImagePath -PassThru
        $nativeImageMounted = $true
        $disk = $mountedImage | Get-Disk
        $partition = $disk | Get-Partition |
            Where-Object { $_.Type -ne 'Reserved' } |
            Sort-Object Size -Descending |
            Select-Object -First 1
        if ($null -eq $partition) {
            throw 'The VHD/VHDX contains no mountable partition.'
        }
        if (-not $partition.DriveLetter) {
            if (Get-Volume -DriveLetter $DriveLetter -ErrorAction SilentlyContinue) {
                throw "Drive letter $DriveLetter`: is already in use"
            }
            $partition | Set-Partition -NewDriveLetter $DriveLetter
            $actualDriveLetter = $DriveLetter.ToUpperInvariant()
        }
        else {
            $actualDriveLetter = $partition.DriveLetter.ToString().ToUpperInvariant()
        }
    }

    $mediaRoot = "$actualDriveLetter`:\"
    if (-not (Test-Path -LiteralPath $mediaRoot -PathType Container)) {
        throw "Mounted filesystem is not accessible at $mediaRoot"
    }
    Write-Host "Virtual disk mounted read-write at $mediaRoot"
    $commandStatus = Invoke-RequestedCommand -MediaRoot $mediaRoot
}
finally {
    if ($vmdkMounted) {
        & $vmwareMount "$DriveLetter`:" '/d' | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "vmware-mount.exe could not disconnect $DriveLetter`:"
        }
    }
    if ($nativeImageMounted) {
        Dismount-DiskImage -ImagePath $fullImagePath
    }
}

exit $commandStatus
