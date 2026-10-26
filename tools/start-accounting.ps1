[CmdletBinding()]
param(
    [string] $Catalog,
    [switch] $NoBrowser
)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repository '.tmp-kernel-venv/Scripts/python.exe'
if (-not $Catalog) {
    $Catalog = Join-Path $repository 'data/accounting/catalog.sqlite3'
}
$catalogPath = [System.IO.Path]::GetFullPath($Catalog)
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw '请先执行 tools/kernel-runtime.ps1 准备仓库运行环境。'
}
if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
    throw "指定目录库不存在：$catalogPath。请核对位置。"
}
$mode = if ($NoBrowser) { 'service-info' } else { 'serve' }
& $python -I -B -X utf8 -m ai_accounting_modular.host.runtime.cli --catalog $catalogPath $mode
if ($LASTEXITCODE -ne 0) {
    throw '记账环境未能就绪，请按上面的诊断处理。'
}
