param(
    [string]$Archive,
    [switch]$NoPath,
    [switch]$Update
)

$ErrorActionPreference = 'Stop'
$repository = 'https://github.com/nyorf/claudex'
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\Claudex'
$installDir = Join-Path $installRoot 'current'
$backup = Join-Path $installRoot 'previous'
$lock = Join-Path $installRoot 'install.lock'

function Stop-Install([string]$Message) {
    throw "Claudex installer: $Message"
}

function Move-InstalledBundle([string]$Source, [string]$Destination) {
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination
            return
        } catch [System.IO.IOException], [System.UnauthorizedAccessException] {
            if (-not (Test-Path -LiteralPath $Source) -or (Test-Path -LiteralPath $Destination)) {
                throw
            }
            Start-Sleep -Milliseconds 500
        }
    }
    Stop-Install 'Close any open Claudex processes and run the installer again.'
}

function Assert-RegularDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Stop-Install 'An installer directory is not a regular directory.'
        }
    }
}

function Assert-Owned([string]$Path) {
    Assert-RegularDirectory $Path
    if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath (Join-Path $Path '.claudex-install') -PathType Leaf)) {
        Stop-Install 'The installation directory belongs to another installation.'
    }
}

function Get-UserPath {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
    try {
        if ($null -ne $key) {
            $value = $key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($null -ne $value) {
                $kind = $key.GetValueKind('Path')
                if ($kind -notin @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) {
                    Stop-Install 'The user PATH has an unsupported registry type.'
                }
                return @{ Value = [string]$value; Kind = $kind; Exists = $true }
            }
        }
        return @{ Value = ''; Kind = [Microsoft.Win32.RegistryValueKind]::ExpandString; Exists = $false }
    } finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Normalize-PathEntry([string]$Value) {
    return [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"')).TrimEnd('\')
}

function Test-PathEntry([string]$Value, [string]$Entry) {
    foreach ($part in ($Value -split ';')) {
        if ((Normalize-PathEntry $part) -ieq (Normalize-PathEntry $Entry)) { return $true }
    }
    return $false
}

function Set-UserPath([string]$Value, [Microsoft.Win32.RegistryValueKind]$Kind, [bool]$Exists) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        if ($Exists) { $key.SetValue('Path', $Value, $Kind) }
        else { $key.DeleteValue('Path', $false) }
    } finally { $key.Dispose() }
    try {
        if (-not ('ClaudexEnvironmentBroadcast' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ClaudexEnvironmentBroadcast {
    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr wParam,
        string lParam, uint flags, uint timeout, out UIntPtr result);
}
'@
        }
        $result = [UIntPtr]::Zero
        [void][ClaudexEnvironmentBroadcast]::SendMessageTimeout([IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, 'Environment', 2, 1000, [ref]$result)
    } catch {
        Write-Warning 'The PATH changed. Restart your terminal to use it.'
    }
}

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Stop-Install 'LOCALAPPDATA is unavailable.'
}
if ($Update) {
    $choice = '2'
} else {
    Write-Host "Claudex installer`n`n1) Install`n2) Update`n3) Remove`n0) Exit`n"
    if ([Console]::IsInputRedirected) {
        Write-Host -NoNewline 'Choice: '
        $choice = [Console]::ReadLine()
    } else {
        $choice = Read-Host 'Choice'
    }
}
if ($choice -eq '0') { exit 0 }
if ($choice -notin @('1', '2', '3')) { Stop-Install 'Choose 1, 2, 3, or 0.' }

Assert-RegularDirectory $installRoot
New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
try {
    $lockStream = [IO.File]::Open($lock, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
} catch {
    Stop-Install 'Another installation may be running. Remove the installer lock if it is stale.'
}
$stage = $null
$installedNew = $false
$pathChanged = $false
$committed = $false
$priorUserPath = $null
try {
    Assert-Owned $backup
    Assert-Owned $installDir
    if (Test-Path -LiteralPath $backup) {
        if (Test-Path -LiteralPath $installDir) { Remove-Item -LiteralPath $backup -Recurse -Force }
        else { Move-Item -LiteralPath $backup -Destination $installDir }
    }
    if ($choice -eq '1' -and (Test-Path -LiteralPath $installDir)) {
        Stop-Install 'Claudex is already installed. Choose Update.'
    }
    if ($choice -in @('2', '3') -and -not (Test-Path -LiteralPath $installDir)) {
        Stop-Install 'Claudex is not installed by this installer. Choose Install.'
    }
    $pathAdded = $false
    $pathOriginallyAbsent = $false
    if (Test-Path -LiteralPath $installDir) {
        $marker = Get-Content -LiteralPath (Join-Path $installDir '.claudex-install') -Raw
        $pathAdded = $marker -match '(?m)^path_added=true\s*$'
        $pathOriginallyAbsent = $marker -match '(?m)^path_originally_absent=true\s*$'
    }
    if ($choice -eq '3') {
        if ($pathAdded -and -not $NoPath) { $priorUserPath = Get-UserPath }
        Move-InstalledBundle $installDir $backup
        if ($pathAdded -and -not $NoPath) {
            $entries = @($priorUserPath.Value -split ';' | Where-Object { (Normalize-PathEntry $_) -ine (Normalize-PathEntry $installDir) })
            $restoredPath = $entries -join ';'
            $keepPath = -not ($pathOriginallyAbsent -and $restoredPath -eq '')
            Set-UserPath $restoredPath $priorUserPath.Kind $keepPath
            $pathChanged = $true
        }
        $committed = $true
        Remove-Item -LiteralPath $backup -Recurse -Force
        Write-Host 'Removed Claudex. Your profiles and configuration remain on this computer.'
        exit 0
    }

    if ($env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'ARM64')) {
        Stop-Install 'This Windows processor is not supported.'
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $stage = Join-Path $installRoot ('.stage-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    if ($Archive) {
        $source = (Resolve-Path -LiteralPath $Archive).Path
        if (-not (Test-Path -LiteralPath "$source.sha256" -PathType Leaf)) {
            Stop-Install 'The local archive and its .sha256 file are required.'
        }
        $archiveName = [IO.Path]::GetFileName($source)
        Copy-Item -LiteralPath $source -Destination (Join-Path $stage $archiveName)
        Copy-Item -LiteralPath "$source.sha256" -Destination (Join-Path $stage 'checksum')
    } else {
        $release = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/nyorf/claudex/releases/latest' -Headers @{ 'User-Agent' = 'claudex-installer' }
        $tag = [string]$release.tag_name
        if ($tag -cnotmatch '^v?\d+\.\d+\.\d+$') { Stop-Install 'The latest release tag is invalid.' }
        $version = $tag -replace '^v', ''
        $archiveName = "claudex-$version-windows-x86_64.zip"
        $releaseUrl = "$repository/releases/download/$tag"
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/$archiveName" -OutFile (Join-Path $stage $archiveName)
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/$archiveName.sha256" -OutFile (Join-Path $stage 'checksum')
    }
    if ($archiveName -cnotmatch '^claudex-(\d+\.\d+\.\d+)-windows-x86_64\.zip$') {
        Stop-Install 'The archive name does not match this system.'
    }
    $version = $Matches[1]
    $line = (Get-Content -LiteralPath (Join-Path $stage 'checksum') -Raw).Trim()
    if ($line -cnotmatch '^([0-9a-fA-F]{64})\s+([^\s]+)$' -or $Matches[2] -cne $archiveName) {
        Stop-Install 'The checksum file does not match the archive.'
    }
    $expected = $Matches[1]
    $archivePath = Join-Path $stage $archiveName
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ine $expected) {
        Stop-Install 'The archive checksum did not match.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('\', '/')
            if (-not ($name -eq 'claudex' -or $name.StartsWith('claudex/')) -or ('/' + $name + '/') -match '/\.\./') {
                Stop-Install 'The archive contains an unsafe path or unexpected layout.'
            }
        }
    } finally { $zip.Dispose() }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $stage
    $bundle = Join-Path $stage 'claudex'
    $executable = Join-Path $bundle 'claudex.exe'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $bundle '_internal') -PathType Container)) {
        Stop-Install 'The release bundle is incomplete.'
    }
    $reported = & $executable --version
    if ($LASTEXITCODE -ne 0 -or $reported -cnotin @("claudex $version", "claudex $version ($repository)")) {
        Stop-Install 'The release executable did not report the expected version.'
    }
    if ($PSCommandPath -and (Test-Path -LiteralPath $PSCommandPath -PathType Leaf)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $bundle 'install.ps1')
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/install.ps1" -OutFile (Join-Path $bundle 'install.ps1')
    }
    if (-not $NoPath) {
        $priorUserPath = Get-UserPath
        if (-not (Test-PathEntry $priorUserPath.Value $installDir)) {
            $pathAdded = $true
            $pathOriginallyAbsent = -not $priorUserPath.Exists
        }
    }
    $marker = "path_added=$($pathAdded.ToString().ToLowerInvariant())`npath_originally_absent=$($pathOriginallyAbsent.ToString().ToLowerInvariant())"
    Set-Content -LiteralPath (Join-Path $bundle '.claudex-install') -Value $marker -Encoding ASCII
    if (Test-Path -LiteralPath $installDir) { Move-InstalledBundle $installDir $backup }
    Move-Item -LiteralPath $bundle -Destination $installDir
    $installedNew = $true
    if ($pathAdded -and -not $NoPath -and -not (Test-PathEntry $priorUserPath.Value $installDir)) {
        $updatedPath = if ($priorUserPath.Value -eq '') { $installDir } else { "$($priorUserPath.Value);$installDir" }
        Set-UserPath $updatedPath $priorUserPath.Kind $true
        $pathChanged = $true
        $env:Path = "$env:Path;$installDir"
    }
    $committed = $true
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
    Write-Host "Claudex $version is installed. Open a new terminal to use claudex."
    Write-Host "To update or remove it later, run & '$installDir\install.ps1'."
} catch {
    if (-not $committed) {
        if ($pathChanged) { Set-UserPath $priorUserPath.Value $priorUserPath.Kind $priorUserPath.Exists }
        if ($installedNew -and (Test-Path -LiteralPath $installDir)) {
            Remove-Item -LiteralPath $installDir -Recurse -Force
        }
        if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $installDir)) {
            Move-Item -LiteralPath $backup -Destination $installDir
        }
    }
    throw
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
    $lockStream.Dispose()
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
}
