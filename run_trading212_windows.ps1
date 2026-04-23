[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$Interactive,
    [string]$LogLevel = "INFO",
    [string]$Symbols = "",
    [switch]$Reinstall,
    [switch]$Headed,
    [switch]$FreshSession
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

function Invoke-PythonLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 @Arguments
        return
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python @Arguments
        return
    }

    throw "Python 3.12 or newer was not found in PATH."
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $envPath = Join-Path $repoRoot ".env"
    if (-not (Test-Path $envPath)) {
        return $null
    }

    foreach ($line in Get-Content $envPath) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($line.TrimStart().StartsWith("#")) {
            continue
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) {
            continue
        }
        if ($parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"')
        }
    }

    return $null
}

function Get-FirefoxCandidate {
    $candidates = @(
        $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "Mozilla Firefox\firefox.exe" }),
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Mozilla Firefox\firefox.exe" }),
        $(if ($env:USERPROFILE) { Join-Path $env:USERPROFILE "Desktop\Firefox.exe" })
    ) | Where-Object { $_ -and (Test-Path $_) }

    if ($candidates.Count -gt 0) {
        return $candidates[0]
    }

    return $null
}

function Test-FirefoxExecutable {
    param(
        [string]$Path
    )

    if (-not $Path) {
        return $false
    }

    if (-not (Test-Path $Path)) {
        return $false
    }

    try {
        $item = Get-Item $Path
        $versionInfo = $item.VersionInfo
        if ($versionInfo.OriginalFilename -ieq "firefox.exe") {
            return $true
        }
        if ($versionInfo.ProductName -match "Firefox") {
            return $true
        }
        if ($versionInfo.FileDescription -match "Firefox") {
            return $true
        }
    } catch {
        return $false
    }

    return $false
}

$venvDir = Join-Path $repoRoot ".venv-win"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    Write-Host "Creating Windows virtual environment at $venvDir"
    Invoke-PythonLauncher -Arguments @("-m", "venv", $venvDir)
}

if ($Reinstall -or -not (Test-Path (Join-Path $venvDir "Scripts\t212-cfd-bot.exe"))) {
    Write-Host "Installing bot into $venvDir"
    & $pythonExe -m pip install --upgrade pip
    & $pythonExe -m pip install -e .
}

if (-not (Test-Path (Join-Path $repoRoot ".env"))) {
    Copy-Item (Join-Path $repoRoot ".env.example") (Join-Path $repoRoot ".env")
    Write-Host "Created .env from .env.example"
}

$env:BROKER = "trading212"

if ($Headed) {
    $env:TRADING212_HEADLESS = "false"
    Write-Host "Running Trading212 with a visible Firefox window"
}

if ($FreshSession) {
    $cookiePath = Join-Path $repoRoot "_cookies.csv"
    if (Test-Path $cookiePath) {
        Remove-Item $cookiePath -Force
        Write-Host "Deleted stale Trading212 cookie cache at $cookiePath"
    }
}

$configuredFirefox = Get-DotEnvValue -Name "FIREFOX_BINARY_PATH"
if ($configuredFirefox) {
    if (Test-FirefoxExecutable -Path $configuredFirefox) {
        $env:FIREFOX_BINARY_PATH = $configuredFirefox
        Write-Host "Using Firefox from configured path $configuredFirefox"
    } else {
        Write-Warning "Configured FIREFOX_BINARY_PATH is not a valid Firefox browser executable: $configuredFirefox"
    }
}

if (-not $env:FIREFOX_BINARY_PATH) {
    $detectedFirefox = Get-FirefoxCandidate
    if ($detectedFirefox -and (Test-FirefoxExecutable -Path $detectedFirefox)) {
        $env:FIREFOX_BINARY_PATH = $detectedFirefox
        Write-Host "Using Firefox from $detectedFirefox for this run"
    } else {
        Write-Warning "Firefox was not auto-detected. Set FIREFOX_BINARY_PATH in .env to the real firefox.exe if Trading212 login cannot find Firefox."
    }
}

$runArgs = @("-m", "t212_cfd_bot", "--log-level", $LogLevel)
if ($Once) {
    $runArgs += "--once"
}
if ($Interactive) {
    $runArgs += "--interactive"
}
if ($Symbols) {
    $runArgs += @("--symbols", $Symbols)
}

& $pythonExe @runArgs
exit $LASTEXITCODE
