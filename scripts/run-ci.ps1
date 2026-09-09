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

    function New-PytestBaseTemp {
        if ($env:OLFACTORYPILOT_PYTEST_BASETEMP) {
            $baseTemp = [System.IO.Path]::GetFullPath($env:OLFACTORYPILOT_PYTEST_BASETEMP)
            $repoPath = [System.IO.Path]::GetFullPath($repoRoot)
            $repoPrefix = $repoPath + [System.IO.Path]::DirectorySeparatorChar
            $insideRepository = $baseTemp.Equals(
                $repoPath,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or $baseTemp.StartsWith(
                $repoPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )
            if ($insideRepository) {
                throw "OLFACTORYPILOT_PYTEST_BASETEMP must be outside the repository: $baseTemp"
            }
            if (Test-Path -LiteralPath $baseTemp) {
                throw "OLFACTORYPILOT_PYTEST_BASETEMP must not already exist: $baseTemp"
            }
            return $baseTemp
        }
        $name = "op-pytest-$PID-$([guid]::NewGuid().ToString('N'))"
        return Join-Path (Split-Path -Parent $repoRoot) $name
    }

    function Invoke-Pytest {
        param(
            [Parameter(Mandatory = $true)]
            [string]$Stage,
            [string[]]$Arguments = @()
        )

        $managedBaseTemp = -not [bool]$env:OLFACTORYPILOT_PYTEST_BASETEMP
        $baseTemp = New-PytestBaseTemp
        $env:OLFACTORYPILOT_EXPECTED_BASETEMP = $baseTemp
        $pythonArguments = @("-m", "pytest") + $Arguments + @("--basetemp", $baseTemp)
        $exitCode = 1
        $cleanupError = $null
        try {
            Write-Host "==> $Stage"
            & python @pythonArguments
            $exitCode = $LASTEXITCODE
        }
        finally {
            if ($managedBaseTemp -and (Test-Path -LiteralPath $baseTemp)) {
                try {
                    Remove-Item -LiteralPath $baseTemp -Recurse -Force
                }
                catch {
                    $cleanupError = $_
                    [Console]::Error.WriteLine(
                        "Failed to clean pytest basetemp '$baseTemp': $($_.Exception.Message)"
                    )
                }
            }
        }
        if ($exitCode -ne 0) {
            [Console]::Error.WriteLine("Stage failed: $Stage (Python exit code $exitCode).")
            exit $exitCode
        }
        if ($null -ne $cleanupError) {
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
