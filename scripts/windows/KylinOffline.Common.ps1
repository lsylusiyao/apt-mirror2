#Requires -Version 5.1

Set-StrictMode -Version Latest

function Invoke-KylinOfflineCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $offlineCommand = Get-Command 'apt-mirror-offline' -ErrorAction SilentlyContinue
    if ($null -ne $offlineCommand) {
        & $offlineCommand @Arguments | Out-Host
        return $LASTEXITCODE
    }

    $pythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pythonLauncher) {
        & $pythonLauncher.Source -3 -m apt_mirror.offline @Arguments | Out-Host
        return $LASTEXITCODE
    }

    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        & $pythonCommand.Source -m apt_mirror.offline @Arguments | Out-Host
        return $LASTEXITCODE
    }

    throw 'apt-mirror-offline or Python 3 was not found. Install this project with Python 3.10+ first.'
}
