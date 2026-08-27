[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$dashboardLauncher = Join-Path $repositoryRoot ".venv\Scripts\finance-dashboard.exe"
$dashboardUrl = "http://127.0.0.1:$Port/"

if (-not (Test-Path -LiteralPath $dashboardLauncher -PathType Leaf)) {
    throw "找不到财务看板启动程序：$dashboardLauncher。请先同步本仓库虚拟环境。"
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$listenerProcessIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
$managedProcesses = @()

foreach ($listenerProcessId in $listenerProcessIds) {
    $listenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerProcessId"
    if ($null -eq $listenerProcess) {
        continue
    }

    $commandLine = [string]$listenerProcess.CommandLine
    if ($commandLine.IndexOf($dashboardLauncher, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "端口 $Port 正被其他程序占用（PID $listenerProcessId），为避免误停进程，已取消重启。"
    }

    $managedProcesses += $listenerProcess
}

foreach ($managedProcess in $managedProcesses) {
    Stop-Process -Id $managedProcess.ProcessId
    Wait-Process -Id $managedProcess.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}

$logDirectory = Join-Path ([IO.Path]::GetTempPath()) "ai-accounting-core"
[IO.Directory]::CreateDirectory($logDirectory) | Out-Null
$stdoutLog = Join-Path $logDirectory "finance-dashboard-$Port.stdout.log"
$stderrLog = Join-Path $logDirectory "finance-dashboard-$Port.stderr.log"

$dashboardProcess = Start-Process `
    -FilePath $dashboardLauncher `
    -ArgumentList "--port", $Port, "--no-open" `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

$dashboardListener = $null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $newListeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($newListener in $newListeners) {
        $newListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($newListener.OwningProcess)"
        if (
            $null -ne $newListenerProcess -and
            ([string]$newListenerProcess.CommandLine).IndexOf(
                $dashboardLauncher,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        ) {
            $dashboardListener = $newListenerProcess
            break
        }
    }

    if ($null -ne $dashboardListener) {
        break
    }
    Start-Sleep -Milliseconds 250
}

if ($null -eq $dashboardListener) {
    throw "财务看板未能在预期时间内就绪。错误日志：$stderrLog"
}

try {
    $response = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 15
}
catch {
    throw "财务看板已经监听端口，但页面检查失败。错误日志：$stderrLog"
}

if ($response.StatusCode -ne 200) {
    throw "财务看板页面检查返回 HTTP $($response.StatusCode)。错误日志：$stderrLog"
}

if ($OpenBrowser) {
    Start-Process $dashboardUrl
}

Write-Host "财务看板已重启：$dashboardUrl"
Write-Host "进程 PID：$($dashboardListener.ProcessId)"
Write-Host "错误日志：$stderrLog"
