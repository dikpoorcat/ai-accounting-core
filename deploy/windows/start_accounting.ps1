[CmdletBinding()]
param(
    [string]$DataRoot,
    [string]$PackageRoot,
    [switch]$NoBrowser,
    # Compatibility with existing shortcuts; opening the page is now the default.
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if ($NoBrowser -and $OpenBrowser) {
    throw 'START_OPTIONS_CONFLICT: NoBrowser 与 OpenBrowser 不能同时使用。'
}
if (-not $DataRoot) {
    $DataRoot = if ($env:FINANCE_DATA_ROOT) { $env:FINANCE_DATA_ROOT } else { Join-Path $repositoryRoot 'data' }
}
$accountingRoot = [System.IO.Path]::GetFullPath($DataRoot)
if ($PackageRoot) {
    $runtimeRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
    $kernelPython = Join-Path $runtimeRoot 'runtime\python.exe'
} else {
    $kernelPython = Join-Path $repositoryRoot '.tmp-kernel-venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $kernelPython -PathType Leaf)) {
    throw 'KERNEL_RUNTIME_MISSING: 请安装本地内核运行包并传入 PackageRoot，或先执行 scripts/kernel-runtime.ps1。'
}

# The managed daemon owns the service lock and starts hidden. This launcher only
# validates the installed runtime, selects the explicit data root and opens its UI.
& $kernelPython -I -X utf8 -c "from ai_accounting.kernel.runtime import require_supported_runtime; from ai_accounting.kernel.http import dashboard_directory; require_supported_runtime(); assert all((dashboard_directory() / name).is_file() for name in ('index.html', 'local.html')), 'KERNEL_UI_MISSING: run npm run build:release in frontend'"
if ($LASTEXITCODE -ne 0) {
    throw 'KERNEL_INSTALLATION_INVALID: 运行时或工作台发布文件不可用，请按本地启动文档完成安装。'
}

Write-Host "正在连接本地会计服务，资料目录：$accountingRoot"
$commandArguments = @('-I', '-X', 'utf8', '-m', 'ai_accounting.kernel.cli', '--root', $accountingRoot)
if ($NoBrowser) {
    $commandArguments += @('security', 'status')
} else {
    $commandArguments += 'serve'
}
& $kernelPython @commandArguments
if ($LASTEXITCODE -ne 0) {
    throw 'KERNEL_START_FAILED: 本地会计服务未能启动，请按上面的诊断处理后重试。'
}
