[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 120,

    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$composeFile = Join-Path $repositoryRoot "docker-compose.yml"
$dashboardLauncher = Join-Path $repositoryRoot ".venv\Scripts\finance-dashboard.exe"

if (-not (Test-Path -LiteralPath $dashboardLauncher -PathType Leaf)) {
    throw "ACCOUNTING_NOT_INSTALLED: 缺少仓库虚拟环境中的看板程序，请先完成本地安装。"
}
if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot "src\ai_accounting\static\dashboard\index.html"))) {
    throw "DASHBOARD_NOT_BUILT: 缺少看板发布文件，请按本地运行文档完成前端构建。"
}
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $dockerCommand) {
    throw "DOCKER_NOT_INSTALLED: 找不到 Docker CLI，请先完成 Docker Desktop 安装。"
}

# Inspect the selected endpoint without changing the user's Docker context.
$endpoint = & docker context inspect --format '{{.Endpoints.docker.Host}}' 2>$null
if ($LASTEXITCODE -ne 0 -or [string]$endpoint -notlike "npipe://*") {
    throw "DOCKER_CONTEXT_NOT_LOCAL: 当前 Docker 上下文不是 Windows 本机管道，请选择本机 Docker Desktop 上下文。"
}
if ($env:DOCKER_HOST -or $env:DOCKER_CONTEXT) {
    throw "DOCKER_CONTEXT_OVERRIDDEN: 当前终端设置了 Docker 连接覆盖，请在未覆盖连接的本机终端启动。"
}

function Test-DockerReady {
    $ErrorActionPreference = "SilentlyContinue"
    & docker info --format '{{.ServerVersion}}' *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-DockerReady)) {
    $desktopCandidates = @(
        (Join-Path (Split-Path (Split-Path $dockerCommand.Source)) "..\Docker Desktop.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe")
    )
    $desktop = $desktopCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $desktop) {
        throw "DOCKER_DESKTOP_NOT_FOUND: Docker Engine 未运行且找不到 Docker Desktop，请修复本机安装。"
    }
    Write-Host "正在启动 Docker Desktop，等待 Docker Engine 就绪……"
    Start-Process -FilePath $desktop -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (-not (Test-DockerReady)) {
        if ((Get-Date) -ge $deadline) {
            throw "DOCKER_START_TIMEOUT: Docker Engine 未在规定时间内就绪，请查看 Docker Desktop 的启动状态。"
        }
        Start-Sleep -Seconds 2
    }
}
Write-Host "Docker Engine 已就绪。"

# Daily startup must reuse installed containers and volumes, never initialize a new database.
$containerIds = @(& docker compose -f $composeFile ps --all --quiet postgres)
if ($LASTEXITCODE -ne 0) {
    throw "POSTGRES_LOOKUP_FAILED: 无法读取本仓库 PostgreSQL 容器状态，请检查 Docker Compose。"
}
if ($containerIds.Count -ne 1 -or [string]::IsNullOrWhiteSpace($containerIds[0])) {
    throw "POSTGRES_CONTAINER_MISSING: 未找到唯一的已有 PostgreSQL 容器，请核对本机安装及 Docker 上下文；未创建新库。"
}
Write-Host "正在启动已有 PostgreSQL 容器并等待健康检查……"
& docker compose -f $composeFile start --wait --wait-timeout $TimeoutSeconds postgres
if ($LASTEXITCODE -ne 0) {
    throw "POSTGRES_START_FAILED: PostgreSQL 未就绪，请检查本仓库 postgres 容器日志。"
}

& (Join-Path $PSScriptRoot "restart_dashboard.ps1") -Port $Port -EnsureRunning -OpenBrowser:$OpenBrowser
Write-Host "本机服务已就绪；MCP 连接和负责人登录仍需通过当前会话的只读工具调用验证。"
