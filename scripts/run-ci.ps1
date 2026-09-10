[CmdletBinding()]
param(
    [ValidateSet("lint", "test-fast", "test", "build", "ci")]
    [string]$Task = "ci"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

try {
    if (-not $env:QT_QPA_PLATFORM) {
        $env:QT_QPA_PLATFORM = "offscreen"
    }

    function Invoke-Python {
        param(
            [Parameter(Mandatory = $true)]
            [string]$Stage,
            [Parameter(Mandatory = $true)]
            [string[]]$Arguments
        )

        Write-Host "==> $Stage"
        & python @Arguments
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            [Console]::Error.WriteLine("Stage failed: $Stage (Python exit code $exitCode).")
            exit $exitCode
        }
    }

    function New-PytestSession {
        $helperPython = if ($env:OLFACTORYPILOT_DEV_PYTHON) {
            $env:OLFACTORYPILOT_DEV_PYTHON
        }
        else {
            "python"
        }
        $helper = Join-Path $repoRoot "scripts/dev_temp.py"
        $json = & $helperPython $helper create --purpose pytest --project-root $repoRoot --owner-pid $PID
        if ($LASTEXITCODE -ne 0) {
            throw "无法创建 pytest owned session。"
        }
        return $json | ConvertFrom-Json
    }

    function Remove-PytestSession {
        param(
            [Parameter(Mandatory = $true)]
            $Session
        )

        $helperPython = if ($env:OLFACTORYPILOT_DEV_PYTHON) {
            $env:OLFACTORYPILOT_DEV_PYTHON
        }
        else {
            "python"
        }
        $helper = Join-Path $repoRoot "scripts/dev_temp.py"
        & $helperPython $helper cleanup --session $Session.path --token $Session.token --owner-pid $PID
        return $LASTEXITCODE
    }

    function Invoke-Pytest {
        param(
            [Parameter(Mandatory = $true)]
            [string]$Stage,
            [string[]]$Arguments = @()
        )

        $session = New-PytestSession
        $baseTemp = $session.basetemp
        $previousSession = $env:OLFACTORYPILOT_DEVTEMP_SESSION
        $previousToken = $env:OLFACTORYPILOT_DEVTEMP_TOKEN
        $previousExpectedBaseTemp = $env:OLFACTORYPILOT_EXPECTED_BASETEMP
        $env:OLFACTORYPILOT_DEVTEMP_SESSION = $session.path
        $env:OLFACTORYPILOT_DEVTEMP_TOKEN = $session.token
        $env:OLFACTORYPILOT_EXPECTED_BASETEMP = $baseTemp
        $pythonArguments = @("-m", "pytest") + $Arguments + @("--basetemp", $baseTemp)
        $exitCode = 1
        $cleanupExitCode = 0
        try {
            Write-Host "==> $Stage"
            & python @pythonArguments
            $exitCode = $LASTEXITCODE
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
            if ($null -eq $previousExpectedBaseTemp) {
                Remove-Item Env:OLFACTORYPILOT_EXPECTED_BASETEMP -ErrorAction SilentlyContinue
            }
            else {
                $env:OLFACTORYPILOT_EXPECTED_BASETEMP = $previousExpectedBaseTemp
            }
            $cleanupExitCode = Remove-PytestSession -Session $session
            if ($cleanupExitCode -ne 0) {
                [Console]::Error.WriteLine("Failed to clean pytest owned session '$($session.path)'.")
            }
        }
        if ($exitCode -ne 0) {
            [Console]::Error.WriteLine("Stage failed: $Stage (Python exit code $exitCode).")
            exit $exitCode
        }
        if ($cleanupExitCode -ne 0) {
            exit 1
        }
    }

    function Invoke-Lint {
        Invoke-Python -Stage "ruff" -Arguments @("-m", "ruff", "check", ".")
    }

    function Invoke-TestFast {
        Invoke-Pytest -Stage "pytest fast" -Arguments @("-m", "not slow")
    }

    function Invoke-Test {
        Invoke-Pytest -Stage "pytest full"
    }

    function Invoke-Build {
        Invoke-Python -Stage "PyInstaller" -Arguments @("-m", "PyInstaller", "--noconfirm", "pyinstaller.spec")
        $requiredArtifacts = @(
            (Join-Path $repoRoot "dist/OlfactoryPilot/OlfactoryPilot.exe"),
            (Join-Path $repoRoot "dist/OlfactoryPilot/_internal/config/default_config.json"),
            (Join-Path $repoRoot "dist/OlfactoryPilot/_internal/docs/ManuelUtilisation_ProgOlfacto.pdf")
        )
        foreach ($artifact in $requiredArtifacts) {
            if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
                [Console]::Error.WriteLine("Missing required PyInstaller artifact: $artifact")
                exit 1
            }
        }
        $files = Get-ChildItem -Path "$repoRoot/dist" -Recurse | Where-Object { -not $_.PSIsContainer }
        $files | Select-Object FullName, Length | Sort-Object Length -Descending | Select-Object -First 5
        $exe = $files | Where-Object { $_.Extension -eq ".exe" } | Select-Object -First 1
        if ($exe) {
            Write-Host "Executable artifact: $($exe.FullName)"
            Write-Host ("Size: {0:N2} MB" -f ($exe.Length / 1MB))
            Get-FileHash $exe.FullName -Algorithm SHA256
        }
    }

    switch ($Task) {
        "lint" { Invoke-Lint }
        "test-fast" { Invoke-TestFast }
        "test" { Invoke-Test }
        "build" { Invoke-Build }
        "ci" {
            Invoke-Lint
            Invoke-Test
            Invoke-Build
        }
    }
}
finally {
    Pop-Location
}
