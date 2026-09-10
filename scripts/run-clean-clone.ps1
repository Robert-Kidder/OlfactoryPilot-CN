[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $repoRoot "scripts/dev_temp.py"
$helperPython = if ($env:OLFACTORYPILOT_DEV_PYTHON) {
    $env:OLFACTORYPILOT_DEV_PYTHON
}
else {
    "python"
}
$bootstrapPython = if ($env:OLFACTORYPILOT_CLEAN_CLONE_PYTHON) {
    $env:OLFACTORYPILOT_CLEAN_CLONE_PYTHON
}
else {
    "python"
}
$session = $null
$resultCode = 0

function Stop-WithNativeCode {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage
    )

    $script:resultCode = $LASTEXITCODE
    if ($script:resultCode -eq 0) {
        $script:resultCode = 1
    }
    throw "阶段失败：$Stage（退出码 $script:resultCode）"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host "==> $Stage"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Stop-WithNativeCode -Stage $Stage
    }
}

function Invoke-CleanClonePytest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    $pytestSession = $null
    $pytestCode = 1
    $cleanupCode = 0
    $previousSession = $env:OLFACTORYPILOT_DEVTEMP_SESSION
    $previousToken = $env:OLFACTORYPILOT_DEVTEMP_TOKEN
    try {
        $pytestSessionJson = & $helperPython $helper create --purpose pytest --project-root $repoRoot --owner-pid $PID
        if ($LASTEXITCODE -ne 0) {
            Stop-WithNativeCode -Stage "创建 clean-clone pytest owned session"
        }
        $pytestSession = $pytestSessionJson | ConvertFrom-Json
        $env:OLFACTORYPILOT_DEVTEMP_SESSION = $pytestSession.path
        $env:OLFACTORYPILOT_DEVTEMP_TOKEN = $pytestSession.token
        Write-Host "==> clean-clone pytest"
        & $Python -m pytest
        $pytestCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previousSession) {
            Remove-Item Env:OLFACTORYPILOT_DEVTEMP_SESSION -ErrorAction SilentlyContinue
        }
        else {
            $env:OLFACTORYPILOT_DEVTEMP_SESSION = $previousSession
        }
        if ($null -eq $previousToken) {
            Remove-Item Env:OLFACTORYPILOT_DEVTEMP_TOKEN -ErrorAction SilentlyContinue
        }
        else {
            $env:OLFACTORYPILOT_DEVTEMP_TOKEN = $previousToken
        }
        if ($null -ne $pytestSession) {
            & $helperPython $helper cleanup --session $pytestSession.path --token $pytestSession.token --owner-pid $PID
            $cleanupCode = $LASTEXITCODE
        }
    }
    if ($pytestCode -ne 0) {
        $script:resultCode = $pytestCode
        if ($cleanupCode -ne 0) {
            [Console]::Error.WriteLine("clean-clone pytest owned session 清理失败：$($pytestSession.path)")
        }
        throw "阶段失败：clean-clone pytest（退出码 $pytestCode）"
    }
    if ($cleanupCode -ne 0) {
        $script:resultCode = 1
        throw "clean-clone pytest owned session 清理失败：$($pytestSession.path)"
    }
}

try {
    $candidateSha = (& git -C $repoRoot rev-parse --verify "HEAD^{commit}").Trim()
    if ($LASTEXITCODE -ne 0 -or $candidateSha -notmatch '^[0-9a-fA-F]{40}$') {
        if ($LASTEXITCODE -ne 0) {
            Stop-WithNativeCode -Stage "解析候选 revision"
        }
        throw "候选 revision 不是完整 commit SHA：$candidateSha"
    }
    Write-Host "Candidate revision: $candidateSha"
    $sessionJson = & $helperPython $helper create --purpose clean-clone --project-root $repoRoot --owner-pid $PID
    if ($LASTEXITCODE -ne 0) {
        Stop-WithNativeCode -Stage "创建 clean-clone owned session"
    }
    $session = $sessionJson | ConvertFrom-Json
    $worktree = Join-Path $session.path "worktree"
    $venv = Join-Path $session.path "venv"

    Invoke-Checked -Stage "克隆 tracked-only worktree" -Command {
        & git clone --no-local --no-hardlinks $repoRoot $worktree
    }
    Invoke-Checked -Stage "检出固定候选 revision" -Command {
        & git -C $worktree checkout --detach $candidateSha
    }
    if (Test-Path -LiteralPath (Join-Path $worktree ".devtmp")) {
        throw "clean clone 不得复制源仓库 .devtmp。"
    }

    Invoke-Checked -Stage "创建隔离 Python venv" -Command {
        & $bootstrapPython -m venv $venv
    }
    $venvPython = Join-Path $venv "Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvPython = Join-Path $venv "Scripts/python.cmd"
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "venv Python 不存在：$venvPython"
    }

    Push-Location $worktree
    try {
        Invoke-Checked -Stage "安装 clean-clone 开发依赖" -Command {
            & $venvPython -m pip install --upgrade pip
            if ($LASTEXITCODE -eq 0) {
                & $venvPython -m pip install -r requirements-dev.txt
            }
        }
        Invoke-Checked -Stage "clean-clone Ruff" -Command {
            & $venvPython -m ruff check .
        }
        Invoke-CleanClonePytest -Python $venvPython
        Invoke-Checked -Stage "clean-clone PyInstaller" -Command {
            & $venvPython -m PyInstaller --noconfirm pyinstaller.spec
        }
        $requiredArtifacts = @(
            (Join-Path $worktree "dist/OlfactoryPilot/OlfactoryPilot.exe"),
            (Join-Path $worktree "dist/OlfactoryPilot/_internal/config/default_config.json"),
            (Join-Path $worktree "dist/OlfactoryPilot/_internal/docs/ManuelUtilisation_ProgOlfacto.pdf")
        )
        foreach ($artifact in $requiredArtifacts) {
            if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
                throw "缺少 clean-clone PyInstaller 产物：$artifact"
            }
        }
        $env:QT_QPA_PLATFORM = "offscreen"
        Invoke-Checked -Stage "offscreen simulation smoke" -Command {
            & $venvPython -c @"
from PySide6.QtCore import QTimer
from app.main import DEFAULT_CONFIG, build_application

app, window = build_application(
    DEFAULT_CONFIG,
    start_worker=True,
    simulation=True,
)
window.show()
QTimer.singleShot(750, app.quit)
result = app.exec()
if not window.controller.lifecycle_stopped():
    raise SystemExit(2)
raise SystemExit(result)
"@
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    if ($resultCode -eq 0) {
        $resultCode = 1
    }
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    if ($null -ne $session) {
        & $helperPython $helper cleanup --session $session.path --token $session.token --owner-pid $PID
        $cleanupCode = $LASTEXITCODE
        if ($cleanupCode -ne 0) {
            [Console]::Error.WriteLine("clean-clone owned session 清理失败：$($session.path)")
            if ($resultCode -eq 0) {
                $resultCode = 1
            }
        }
    }
}

exit $resultCode
