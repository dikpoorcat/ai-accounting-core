[CmdletBinding()]
param(
    [switch] $SkipDependencies,
    [string] $UvPath
)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$kernelEnvironment = Join-Path $repository '.tmp-kernel-venv'
$kernelPython = Join-Path $kernelEnvironment 'Scripts/python.exe'
if ($UvPath) {
    $repositoryUv = (Resolve-Path -LiteralPath $UvPath).Path
} else {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    $repositoryUv = if ($uvCommand) { $uvCommand.Source } else { Join-Path $repository '.venv/Scripts/uv.exe' }
}
$pinnedPython = '3.12.13'

if (-not (Test-Path -LiteralPath $repositoryUv -PathType Leaf)) {
    throw 'Install uv 0.12.3 and add it to PATH, or pass -UvPath. No previous Python environment is required.'
}

# uv 0.12.3 pins the available standalone build; its CPython 3.12.13 Windows
# build supplies SQLite 3.53.1. Do not alter the existing .venv or system Python.
$uvVersion = & $repositoryUv --version
if ($LASTEXITCODE -ne 0 -or $uvVersion -notmatch '^uv 0\.12\.3(?:\s|$)') {
    throw 'This runtime bootstrap requires the repository-pinned uv 0.12.3.'
}

$previousRegistryPreference = $env:UV_PYTHON_NO_REGISTRY
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PYTHON_NO_REGISTRY = '1'
    $env:UV_PROJECT_ENVIRONMENT = $kernelEnvironment
    if (-not (Test-Path -LiteralPath $kernelPython -PathType Leaf)) {
        & $repositoryUv venv $kernelEnvironment --managed-python --python $pinnedPython
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the controlled kernel environment.' }
    }
    & $kernelPython -c 'import sqlite3, sys; assert sys.version_info[:3] == (3, 12, 13), sys.version; assert sqlite3.sqlite_version_info >= (3, 51, 3), sqlite3.sqlite_version; print("Python", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'
    if ($LASTEXITCODE -ne 0) {
        throw 'The existing kernel environment does not meet the pinned runtime contract.'
    }
    if (-not $SkipDependencies) {
        & $repositoryUv sync --project $repository --locked --extra dev --python $kernelPython
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install kernel development dependencies.' }
    }
    Write-Output "Kernel Python: $kernelPython"
}
finally {
    $env:UV_PYTHON_NO_REGISTRY = $previousRegistryPreference
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
}
