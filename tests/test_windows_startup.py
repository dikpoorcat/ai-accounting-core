"""Exercise Windows service orchestration with fake Docker and dashboard processes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL, reason="Windows PowerShell 7 is required"
)
REPOSITORY = Path(__file__).resolve().parents[1]


def run_script(root: Path, source: str) -> subprocess.CompletedProcess[str]:
    harness = root / "harness.ps1"
    harness.write_text(source, encoding="utf-8")
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-File", str(harness)],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )


@pytest.fixture
def installed_repo(tmp_path: Path) -> Path:
    for name in (
        "deploy/windows",
        ".venv/Scripts",
        "src/ai_accounting/static/dashboard",
        "DockerDesktop/resources/bin",
    ):
        (tmp_path / name).mkdir(parents=True)
    for name in (
        ".venv/Scripts/finance-dashboard.exe",
        ".venv/Scripts/python.exe",
        "src/ai_accounting/static/dashboard/index.html",
        "DockerDesktop/Docker Desktop.exe",
    ):
        (tmp_path / name).touch()
    shutil.copy(
        REPOSITORY / "deploy/windows/start_accounting.ps1",
        tmp_path / "deploy/windows/start_accounting.ps1",
    )
    (tmp_path / "deploy/windows/restart_dashboard.ps1").write_text(
        "param($Port, [switch]$EnsureRunning, [switch]$OpenBrowser)\n"
        'Write-Output "DASHBOARD:$Port ENSURE:$EnsureRunning OPEN:$OpenBrowser"\n',
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    ("scenario", "error_code"),
    [
        ("running", None),
        ("cold", None),
        ("missing", "POSTGRES_CONTAINER_MISSING"),
        ("unhealthy", "POSTGRES_START_FAILED"),
        ("schema_outdated", "DATABASE_DEPLOYMENT_NOT_READY"),
        ("remote", "DOCKER_CONTEXT_NOT_LOCAL"),
        ("override", "DOCKER_CONTEXT_OVERRIDDEN"),
    ],
)
def test_startup_reuses_only_existing_local_services(installed_repo, scenario, error_code):
    source = r'''
$ErrorActionPreference = "Stop"
$env:DOCKER_HOST = $null
$env:DOCKER_CONTEXT = $null
$global:infoCalls = 0
$schemaCheckerPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Set-Item -LiteralPath "Function:$schemaCheckerPath" -Value {
    if (($args -join ' ') -ne '-m ai_accounting.company_cli check-schema') {
        throw 'UNEXPECTED_SCHEMA_COMMAND'
    }
    Write-Output 'SCHEMA_CHECKED_READ_ONLY'
    $global:LASTEXITCODE = [int]($scenario -eq 'schema_outdated')
}
function Get-Command {
    param($Name, $ErrorAction)
    [pscustomobject]@{Source = (Join-Path $PSScriptRoot 'DockerDesktop/resources/bin/docker.exe')}
}
function Start-Process {
    param($FilePath, $WindowStyle)
    Write-Output "DESKTOP_STARTED:$WindowStyle"
}
function docker {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'context') {
        if ($scenario -eq 'remote') { return 'ssh://remote' }
        return 'npipe:////./pipe/dockerDesktopLinuxEngine'
    }
    if ($args[0] -eq 'info') {
        $global:infoCalls++
        if ($scenario -eq 'cold' -and $global:infoCalls -eq 1) { $global:LASTEXITCODE = 1 }
        return
    }
    if ($args -contains 'ps') {
        if ($scenario -ne 'missing') { return 'existing-container' }
        return
    }
    if ($args -contains 'start') {
        Write-Output 'EXISTING_CONTAINER_STARTED'
        if ($scenario -eq 'unhealthy') { $global:LASTEXITCODE = 1 }
        return
    }
    throw "Unexpected Docker operation: $args"
}
if ($scenario -eq 'override') { $env:DOCKER_HOST = 'tcp://remote:2375' }
try {
    & ./deploy/windows/start_accounting.ps1 -Port 8877 -OpenBrowser
} catch {
    Write-Output $_.Exception.Message
    exit 1
}
'''
    result = run_script(installed_repo, f"$scenario = '{scenario}'\n" + source)
    output = result.stdout + result.stderr
    if error_code:
        assert result.returncode == 1, output
        assert error_code in output
        assert "DASHBOARD:" not in output
        if scenario not in {"unhealthy", "schema_outdated"}:
            assert "EXISTING_CONTAINER_STARTED" not in output
        if scenario == "schema_outdated":
            assert "SCHEMA_CHECKED_READ_ONLY" in output
    else:
        assert result.returncode == 0, output
        assert "EXISTING_CONTAINER_STARTED" in output
        assert "SCHEMA_CHECKED_READ_ONLY" in output
        assert "DASHBOARD:8877 ENSURE:True OPEN:True" in output
        assert ("DESKTOP_STARTED:Hidden" in output) == (scenario == "cold")


@pytest.mark.parametrize("foreign_listener", [False, True])
def test_ensure_dashboard_never_restarts_existing_listener(installed_repo, foreign_listener):
    shutil.copy(
        REPOSITORY / "deploy/windows/restart_dashboard.ps1",
        installed_repo / "deploy/windows/restart_dashboard.ps1",
    )
    source = r'''
$ErrorActionPreference = "Stop"
function Get-NetTCPConnection {
    param($LocalPort, $State, $ErrorAction)
    [pscustomobject]@{OwningProcess = 1234}
}
function Get-CimInstance {
    param($ClassName, $Filter)
    $line = Join-Path $PSScriptRoot '.venv\Scripts\finance-dashboard.exe'
    if ($foreign) { $line = 'unrelated-server.exe' }
    [pscustomobject]@{ProcessId = 1234; CommandLine = $line}
}
function Stop-Process { throw 'UNEXPECTED_STOP' }
function Start-Process { throw 'UNEXPECTED_START' }
function Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    Write-Host 'PAGE_CHECKED'
    [pscustomobject]@{StatusCode = 200}
}
try {
    & ./deploy/windows/restart_dashboard.ps1 -EnsureRunning
} catch {
    Write-Output $_.Exception.Message
    exit 1
}
'''
    boolean = "$true" if foreign_listener else "$false"
    result = run_script(installed_repo, f"$foreign = {boolean}\n" + source)
    output = result.stdout + result.stderr
    assert "UNEXPECTED_" not in output
    assert result.returncode == (1 if foreign_listener else 0), output
    assert ("PAGE_CHECKED" in output) != foreign_listener
    if foreign_listener:
        assert "1234" in output
