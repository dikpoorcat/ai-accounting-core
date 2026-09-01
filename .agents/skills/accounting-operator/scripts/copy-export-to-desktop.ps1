param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedSha256,

    [string]$FileName,

    [string]$DestinationDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$source = Get-Item -LiteralPath $SourcePath -Force
if ($source.PSIsContainer -or $null -ne $source.LinkType) {
    throw 'DESKTOP_EXPORT_SOURCE_MUST_BE_REGULAR_FILE'
}

$sourceFullPath = [IO.Path]::GetFullPath($source.FullName)
$sourceHash = (Get-FileHash -LiteralPath $sourceFullPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sourceHash -ne $ExpectedSha256.ToLowerInvariant()) {
    throw 'DESKTOP_EXPORT_SOURCE_HASH_MISMATCH'
}

if ([string]::IsNullOrWhiteSpace($FileName)) {
    $FileName = $source.Name
}
if ([IO.Path]::GetFileName($FileName) -ne $FileName) {
    throw 'DESKTOP_EXPORT_FILE_NAME_INVALID'
}

if ([string]::IsNullOrWhiteSpace($DestinationDirectory)) {
    $DestinationDirectory = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::DesktopDirectory
    )
}
if ([string]::IsNullOrWhiteSpace($DestinationDirectory)) {
    throw 'DESKTOP_DIRECTORY_UNAVAILABLE'
}

$destinationRoot = [IO.Path]::GetFullPath($DestinationDirectory)
if (-not (Test-Path -LiteralPath $destinationRoot -PathType Container)) {
    throw 'DESKTOP_DIRECTORY_NOT_FOUND'
}

$destinationPath = [IO.Path]::GetFullPath([IO.Path]::Combine($destinationRoot, $FileName))
$normalizedDestinationRoot = [IO.Path]::TrimEndingDirectorySeparator($destinationRoot)
if ([IO.Path]::GetDirectoryName($destinationPath) -ne $normalizedDestinationRoot) {
    throw 'DESKTOP_EXPORT_DESTINATION_OUTSIDE_DESKTOP'
}

$status = 'copied'
if (Test-Path -LiteralPath $destinationPath) {
    $destination = Get-Item -LiteralPath $destinationPath -Force
    if ($destination.PSIsContainer -or $null -ne $destination.LinkType) {
        throw 'DESKTOP_EXPORT_DESTINATION_MUST_BE_REGULAR_FILE'
    }
    $destinationHash = (
        Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($destinationHash -ne $sourceHash) {
        throw 'DESKTOP_EXPORT_COLLISION'
    }
    $status = 'reused'
}
else {
    Copy-Item -LiteralPath $sourceFullPath -Destination $destinationPath
}

$verifiedHash = (
    Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($verifiedHash -ne $sourceHash) {
    throw 'DESKTOP_EXPORT_DESTINATION_HASH_MISMATCH'
}

[ordered]@{
    status = $status
    file_name = $FileName
    file_path = $destinationPath
    sha256 = $verifiedHash
} | ConvertTo-Json -Compress
