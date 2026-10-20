# Builds a portable, no-install Windows package of NodeCode FEA:
#   release\nodecode-fea-portable-<version>.zip
# containing the built editor, the backend, an embeddable Python with the dependencies (and Tk, for the native
# file dialog) and start.bat. Colleagues unzip it anywhere and double-click start.bat.
#
# Needs on the machine that builds it: Node.js (npm), a 64-bit CPython 3.10+ with pip, and internet access the
# first time (python.org's embeddable Python and the PyPI wheels; the Python zip is cached in release\cache).
#
#   powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1 [-Python C:\path\to\python.exe] [-SkipFrontend]

param(
    [string]$Python = "python",
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$release = Join-Path $repo "release"
$cache = Join-Path $release "cache"
$stage = Join-Path $release "nodecode-fea"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

# --- host Python: its exact version decides which embeddable Python we ship ---------------------------------
$info = & $Python -c "import sys, struct; print('%d.%d.%d' % sys.version_info[:3]); print(struct.calcsize('P') * 8); print(sys.base_prefix)"
Check "Running $Python"
$pyVersion, $bits, $hostPrefix = $info
if ($bits -ne "64") { throw "$Python is $bits-bit; use a 64-bit Python." }
$major, $minor, $null = $pyVersion.Split(".")
if ([int]$major -ne 3 -or [int]$minor -lt 10) { throw "Python 3.10+ is needed, $Python is $pyVersion." }
$tag = "$major$minor"
Step "Using Python $pyVersion from $hostPrefix"

$version = (git -C $repo describe --tags --always --dirty 2>$null)
if (-not $version) { $version = Get-Date -Format "yyyyMMdd" }

# --- 1. editor ----------------------------------------------------------------------------------------------
$frontend = Join-Path $repo "frontend"
if (-not $SkipFrontend) {
    Step "Building the editor"
    Push-Location $frontend
    try {
        if (-not (Test-Path "node_modules")) { npm install --no-audit --no-fund; Check "npm install" }
        npm run build; Check "npm run build"
    } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $frontend "dist\index.html"))) { throw "frontend\dist is missing; build the editor first." }

# --- 2. fresh staging folder --------------------------------------------------------------------------------
Step "Staging in $stage"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage, $cache | Out-Null

# Only the built editor: without frontend\package.json the app never tries to rebuild it.
New-Item -ItemType Directory -Force (Join-Path $stage "frontend") | Out-Null
Copy-Item -Recurse (Join-Path $frontend "dist") (Join-Path $stage "frontend\dist")

# The backend package (no tests, sample inputs or caches). app.py finds the editor at ..\..\frontend\dist.
New-Item -ItemType Directory -Force (Join-Path $stage "backend") | Out-Null
Copy-Item -Recurse (Join-Path $repo "backend\nodecode_fea") (Join-Path $stage "backend\nodecode_fea")
Get-ChildItem -Recurse -Directory -Filter "__pycache__" (Join-Path $stage "backend") | Remove-Item -Recurse -Force
Copy-Item (Join-Path $repo "README.md") $stage
Copy-Item (Join-Path $repo "LICENSE") $stage
Copy-Item (Join-Path $repo "NOTICE") $stage

# --- 3. embeddable Python -----------------------------------------------------------------------------------
$pyZipName = "python-$pyVersion-embed-amd64.zip"
$pyZip = Join-Path $cache $pyZipName
if (-not (Test-Path $pyZip)) {
    Step "Downloading $pyZipName"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyVersion/$pyZipName" -OutFile "$pyZip.part" -UseBasicParsing
    Move-Item -Force "$pyZip.part" $pyZip
}
$pyDir = Join-Path $stage "python"
Expand-Archive $pyZip $pyDir
# The ._pth file fixes sys.path: the stdlib zip, Lib (tkinter), site-packages and the backend.
Set-Content -Encoding ascii (Join-Path $pyDir "python$tag._pth") @(
    "python$tag.zip", ".", "Lib", "Lib\site-packages", "..\backend", "import site")

# --- 4. dependencies, read from backend\pyproject.toml ------------------------------------------------------
$deps = & $Python -c "import tomllib, sys; print('\n'.join(tomllib.load(open(sys.argv[1], 'rb'))['project']['dependencies']))" (Join-Path $repo "backend\pyproject.toml")
if ($LASTEXITCODE -ne 0) {  # Python 3.10 has no tomllib
    $deps = @("fastapi>=0.110", "uvicorn>=0.27", "numpy>=1.24")
}
Step "Installing $($deps -join ', ')"
& $Python -m pip install --disable-pip-version-check --no-warn-script-location --only-binary=:all: `
    --platform win_amd64 --python-version "$major.$minor" --implementation cp `
    --target (Join-Path $pyDir "Lib\site-packages") @deps
Check "pip install"

# --- 5. Tk (the embeddable Python has none; without it the Open ODB dialog falls back to the in-app browser) -
Step "Adding tkinter"
Copy-Item -Recurse (Join-Path $hostPrefix "Lib\tkinter") (Join-Path $pyDir "Lib\tkinter")
foreach ($f in "_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll") {
    $src = Join-Path $hostPrefix "DLLs\$f"
    if (Test-Path $src) { Copy-Item $src $pyDir }  # zlib1.dll exists only in newer builds (Tcl needs it there)
}
Copy-Item -Recurse (Join-Path $hostPrefix "tcl") (Join-Path $pyDir "tcl")
foreach ($d in "tcl\tix8.4.3", "tcl\tcl8.6\tzdata", "Lib\tkinter\test") {
    $p = Join-Path $pyDir $d; if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem -Recurse -Directory -Filter "__pycache__" $pyDir | Remove-Item -Recurse -Force

# --- 6. launcher and instructions ---------------------------------------------------------------------------
Set-Content -Encoding ascii (Join-Path $stage "start.bat") @'
@echo off
rem NodeCode FEA: starts the editor on http://127.0.0.1:8000 and opens it in the browser.
rem Close this window to stop it.
rem If Abaqus isn't found, uncomment and fix the next line:
rem set ABAQUS_CMD=C:\SIMULIA\Commands\abaqus.bat
cd /d "%~dp0"
"%~dp0python\python.exe" -m nodecode_fea.app --no-build --open %*
if errorlevel 1 pause
'@

Set-Content -Encoding utf8 (Join-Path $stage "HOW-TO.txt") @"
NodeCode FEA ($version), portable

1. Unzip this folder anywhere you can write to (e.g. Documents). No installation or admin rights are needed.
2. Double-click start.bat. A console window opens and the editor opens in your browser (http://127.0.0.1:8000).
   Keep the console window open while you work; close it to stop the app.
3. Try the example graph, which uses a built-in mock model (no Abaqus needed). For your own results, put the
   full path of an .odb in an Open ODB node, or click its folder button.

Abaqus: the app looks for the Abaqus launcher on PATH and in the usual SIMULIA folders. If it isn't found,
edit start.bat and set ABAQUS_CMD (e.g. C:\SIMULIA\Commands\abaqus.bat).

Port already in use? Run:  start.bat --port 8001

Your graphs are saved in the browser (per browser and port). Use the editor's Save button to keep them as
files, e.g. before switching browsers or ports.

Updating: unzip the new version over this folder (or next to it) and start it the same way.
See README.md for the full documentation.
"@

# --- 7. smoke test and zip ----------------------------------------------------------------------------------
Step "Checking the packaged app imports"
& (Join-Path $pyDir "python.exe") -c "import nodecode_fea.app as a, tkinter, _tkinter; assert (a.FRONTEND / 'index.html').exists(), a.FRONTEND; assert not a.frontend_stale(); print('ok')"
Check "Smoke test"

$zip = Join-Path $release "nodecode-fea-portable-$version.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Step "Zipping"
Push-Location $release
try { tar.exe -a -c -f $zip "nodecode-fea"; Check "tar" } finally { Pop-Location }
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Step "Done: $zip ($mb MB)"
