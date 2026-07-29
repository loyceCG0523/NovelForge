$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$consoleUrl = "http://127.0.0.1:3900"

try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$consoleUrl/api/status" -TimeoutSec 2
    if ($response.StatusCode -eq 200) {
        try {
            Invoke-RestMethod -Method Post -Uri "$consoleUrl/api/all/start" -TimeoutSec 8 | Out-Null
        } catch {
            # If another operation is active, open the existing console only.
        }
        Start-Process $consoleUrl
        exit 0
    }
} catch {
}

$candidates = @(
    $env:NOVELFORGE_PYTHON,
    (Join-Path $projectRoot ".venv\Scripts\python.exe"),
    "D:\Anaconda3\envs\novelforge-api\python.exe"
)

$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        $python = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}

if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $python = $pythonCommand.Source
    }
}

if (-not $python) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "NovelForge Python environment was not found. Set NOVELFORGE_PYTHON and try again.",
        "NovelForge startup failed"
    ) | Out-Null
    exit 1
}

$supervisor = Join-Path $projectRoot "tools\supervisor\app.py"
$logDirectory = Join-Path $projectRoot ".runtime\supervisor\logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdoutLog = Join-Path $logDirectory "launcher.stdout.log"
$stderrLog = Join-Path $logDirectory "launcher.stderr.log"
Start-Process `
    -FilePath $python `
    -ArgumentList "`"$supervisor`"", "--auto-start" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog

$deadline = (Get-Date).AddSeconds(22.5)
do {
    Start-Sleep -Milliseconds 300
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$consoleUrl/api/status" -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process $consoleUrl
            exit 0
        }
    } catch {
    }
} while ((Get-Date) -lt $deadline)

Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show(
    "NovelForge console failed to start. Check .runtime\supervisor\logs\launcher.stderr.log.",
    "NovelForge startup failed"
) | Out-Null
exit 1
