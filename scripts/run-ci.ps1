[CmdletBinding()]
param(
    [ValidateSet("lint", "test", "build", "ci")]
    [string]$Task = "ci"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

try {
    if (-not $env:QT_QPA_PLATFORM) {
        $env:QT_QPA_PLATFORM = "offscreen"
    }

    function Invoke-Lint {
        python -m ruff check app tests
    }

    function Invoke-Test {
        python -m pytest
    }

    function Invoke-Build {
        python -m PyInstaller pyinstaller.spec
        $files = Get-ChildItem -Path "$repoRoot/dist" -Recurse | Where-Object { -not $_.PSIsContainer }
        $files | Select-Object FullName, Length | Sort-Object Length -Descending | Select-Object -First 5
        $exe = $files | Where-Object { $_.Extension -eq ".exe" } | Select-Object -First 1
        if ($exe) {
            Write-Host "Exe artifact: $($exe.FullName)"
            Write-Host ("Size: {0:N2} MB" -f ($exe.Length / 1MB))
            Get-FileHash $exe.FullName -Algorithm SHA256
        }
    }

    switch ($Task) {
        "lint" { Invoke-Lint }
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
