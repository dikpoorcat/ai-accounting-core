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

$normalizedDestinationRoot = [IO.Path]::TrimEndingDirectorySeparator($destinationRoot)
$baseName = [IO.Path]::GetFileNameWithoutExtension($FileName)
$extension = [IO.Path]::GetExtension($FileName)
$number = 1
$temporaryPath = $null
try {
    while ($true) {
        $candidateName = if ($number -eq 1) { $FileName } else { "$baseName ($number)$extension" }
        $destinationPath = [IO.Path]::GetFullPath([IO.Path]::Combine($destinationRoot, $candidateName))
        if ([IO.Path]::GetDirectoryName($destinationPath) -ne $normalizedDestinationRoot) {
            throw 'DESKTOP_EXPORT_DESTINATION_OUTSIDE_DESKTOP'
        }
        $destination = Get-Item -LiteralPath $destinationPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $destination) {
            if (-not $destination.PSIsContainer -and $null -eq $destination.LinkType) {
                $destinationHash = (
                    Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
                ).Hash.ToLowerInvariant()
                if ($destinationHash -eq $sourceHash) {
                    $status = 'reused'
                    break
                }
            }
            $number++
            continue
        }
        if ($null -eq $temporaryPath) {
            $candidateTemporaryPath = [IO.Path]::Combine(
                $destinationRoot, ".finance-export-$([Guid]::NewGuid().ToString('N')).tmp"
            )
            $output = [IO.File]::Open(
                $candidateTemporaryPath, [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write, [IO.FileShare]::None
            )
            $temporaryPath = $candidateTemporaryPath
            try {
                $inputStream = [IO.File]::OpenRead($sourceFullPath)
                try { $inputStream.CopyTo($output) }
                finally { $inputStream.Dispose() }
            }
            finally { $output.Dispose() }
            $stagedHash = (
                Get-FileHash -LiteralPath $temporaryPath -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            if ($stagedHash -ne $sourceHash) {
                throw 'DESKTOP_EXPORT_SOURCE_HASH_MISMATCH'
            }
        }
        try {
            [IO.File]::Move($temporaryPath, $destinationPath)
            $status = 'copied'
            break
        }
        catch [IO.IOException] {
            $occupied = Get-Item -LiteralPath $destinationPath -Force -ErrorAction SilentlyContinue
            if ($null -ne $occupied) {
                continue
            }
            throw
        }
    }
}
finally {
    if ($null -ne $temporaryPath -and [IO.File]::Exists($temporaryPath)) {
        [IO.File]::Delete($temporaryPath)
    }
}

$verifiedHash = (
    Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($verifiedHash -ne $sourceHash) {
    throw 'DESKTOP_EXPORT_DESTINATION_HASH_MISMATCH'
}

[ordered]@{
    status = $status
    file_name = $candidateName
    file_path = $destinationPath
    sha256 = $verifiedHash
} | ConvertTo-Json -Compress
