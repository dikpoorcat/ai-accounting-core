[CmdletBinding()]
param(
    [string] $OutputDirectory,
    [switch] $SkipValidation
)

$ErrorActionPreference = 'Stop'
$packageRepository = Split-Path -Parent $PSScriptRoot
$packagePython = Join-Path $packageRepository '.tmp-kernel-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $packagePython -PathType Leaf)) {
    throw 'Run scripts/kernel-runtime.ps1 first to prepare the controlled runtime.'
}
if (-not $OutputDirectory) {
    $packageStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutputDirectory = Join-Path $packageRepository ".tmp-local-distribution/$packageStamp"
}
$packageArguments = @(
    (Join-Path $PSScriptRoot 'package_local_kernel.py'),
    '--output', $OutputDirectory
)
if ($SkipValidation) { $packageArguments += '--skip-validation' }
& $packagePython @packageArguments
if ($LASTEXITCODE -ne 0) { throw 'Local runtime packaging or relocation verification failed.' }
