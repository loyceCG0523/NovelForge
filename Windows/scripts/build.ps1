$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distDir = Join-Path $projectRoot "dist"
$buildDir = Join-Path $projectRoot "build"
$specDir = Join-Path $projectRoot ".runtime\pyinstaller-spec"
$releaseDir = Join-Path $projectRoot "release"
$iconPath = Join-Path $projectRoot "assets\novelforge.ico"
$versionFile = Join-Path $projectRoot "packaging\version_info.txt"
$installerScript = Join-Path $projectRoot "packaging\NovelForge.iss"
$frontendDir = Join-Path $projectRoot "frontend"
$frontendOutput = Join-Path $frontendDir "out"
$backendApiDir = Join-Path $projectRoot "backend\api"
$backendWorkerDir = Join-Path $projectRoot "backend\worker"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Run the Windows launcher once to create .venv before packaging."
}

function Reset-GeneratedDirectory([string]$target) {
    $fullTarget = [IO.Path]::GetFullPath($target)
    $rootPrefix = $projectRoot.TrimEnd('\') + '\'
    if (-not $fullTarget.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a directory outside the project: $fullTarget"
    }
    if (Test-Path -LiteralPath $fullTarget) {
        Remove-Item -LiteralPath $fullTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Path $fullTarget -Force | Out-Null
}

function Find-InnoCompiler {
    $candidates = @(
        $env:INNO_SETUP_COMPILER,
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    throw "Inno Setup was not found. Run: winget install JRSoftware.InnoSetup"
}

$versionMatch = Select-String -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"$'
if (-not $versionMatch) {
    throw "Could not read the version from pyproject.toml."
}
$appVersion = $versionMatch.Matches[0].Groups[1].Value

Push-Location $projectRoot
try {
    Push-Location $frontendDir
    try {
        & npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "Failed to install frontend dependencies." }
        & npm run test:markdown-import
        if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
        & npm run build
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $frontendOutput "index.html"))) {
            throw "Failed to build the desktop Web frontend."
        }
    } finally {
        Pop-Location
    }

    & $python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install build dependencies." }
    & $python -m pytest tests -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) { throw "Windows application tests failed." }

    & $python (Join-Path $projectRoot "scripts\make_icon.py")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $iconPath)) {
        throw "Failed to generate the application icon."
    }

    Reset-GeneratedDirectory $distDir
    Reset-GeneratedDirectory $buildDir
    Reset-GeneratedDirectory $specDir
    Reset-GeneratedDirectory $releaseDir

    $pyInstallerArgs = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name", "NovelForge",
        "--icon", $iconPath,
        "--version-file", $versionFile,
        "--add-data", "$iconPath;assets",
        "--add-data", "$frontendOutput;frontend/out",
        "--add-data", "$(Join-Path $backendApiDir 'app\resources');backend/api/app/resources",
        "--distpath", $distDir,
        "--workpath", $buildDir,
        "--specpath", $specDir,
        "--paths", (Join-Path $projectRoot "src"),
        "--paths", $backendApiDir,
        "--paths", $backendWorkerDir,
        "--collect-submodules", "app",
        "--collect-submodules", "worker",
        "--collect-submodules", "langgraph",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.loops.asyncio"
    )
    $pythonBase = (& $python -c "import sys; print(sys.base_prefix)").Trim()
    foreach ($dllName in @("libexpat.dll", "ffi.dll")) {
        $dllPath = Join-Path $pythonBase "Library\bin\$dllName"
        if (Test-Path -LiteralPath $dllPath) {
            $pyInstallerArgs += @("--add-binary", "$dllPath;.")
        }
    }
    $pyInstallerArgs += (Join-Path $projectRoot "src\novelforge_windows\__main__.py")
    & $python @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    # Conda's ICU 58 shadows Windows ICU and is ABI-incompatible with PySide6 Qt 6.11.
    $internalDir = Join-Path $distDir "NovelForge\_internal"
    foreach ($incompatibleIcu in @("icuuc.dll", "icudt58.dll")) {
        $icuPath = Join-Path $internalDir $incompatibleIcu
        if (Test-Path -LiteralPath $icuPath) {
            Remove-Item -LiteralPath $icuPath -Force
        }
    }

    # Qt ships OpenSSL DLLs with the same names as the active Python runtime.
    # _ssl.pyd must use the matching runtime copies or the packaged app cannot start.
    foreach ($opensslDll in @("libcrypto-3-x64.dll", "libssl-3-x64.dll")) {
        $runtimeDll = Join-Path $pythonBase "Library\bin\$opensslDll"
        if (-not (Test-Path -LiteralPath $runtimeDll)) {
            throw "Required Python runtime DLL was not found: $runtimeDll"
        }
        Copy-Item -LiteralPath $runtimeDll -Destination (Join-Path $internalDir $opensslDll) -Force
    }

    $innoCompiler = Find-InnoCompiler
    & $innoCompiler "/DMyAppVersion=$appVersion" $installerScript
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }

    $installer = Join-Path $releaseDir "NovelForge-Windows-x64-Setup.exe"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Installer output was not found: $installer"
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$installer.sha256" -Value "$hash  NovelForge-Windows-x64-Setup.exe" -Encoding ascii
    Get-Item -LiteralPath $installer | Select-Object FullName, Length, LastWriteTime
    Write-Host "SHA256: $hash"
} finally {
    Pop-Location
}
