#Requires -Version 5.1
<#
.SYNOPSIS
    HandVision Release Builder - Embedded Python Distribution
.DESCRIPTION
    Builds a fully standalone HandVision distribution:
    1. Downloads Python 3.12 embeddable package
    2. Bootstraps pip and installs dependencies
    3. Publishes C# WPF as self-contained
    4. Bundles everything into dist/HandVision/
.NOTES
    Run from project root: .\build_release.ps1
#>

param(
    [switch]$SkipPython,
    [switch]$SkipDotnet,
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============================================
# CONFIGURATION
# ============================================

$PYTHON_VERSION     = "3.12.10"
$PYTHON_MAJOR_MINOR = "312"
$PYTHON_URL         = "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip"
$GET_PIP_URL        = "https://bootstrap.pypa.io/get-pip.py"

$PROJECT_ROOT       = $PSScriptRoot
$DIST_DIR           = Join-Path $PROJECT_ROOT "dist"
$OUTPUT_DIR         = Join-Path $DIST_DIR "HandVision"
$PYTHON_DIR         = Join-Path $OUTPUT_DIR "python"
$TEMP_DIR           = Join-Path $PROJECT_ROOT ".build_temp"

$PYTHON_ZIP         = Join-Path $TEMP_DIR "python-embed.zip"
$GET_PIP_FILE       = Join-Path $TEMP_DIR "get-pip.py"

$PIP_PACKAGES       = @(
    "opencv-python-headless"
    "mediapipe"
    "numpy"
)

# ============================================
# HELPER FUNCTIONS
# ============================================

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "============================================" -ForegroundColor DarkCyan
    Write-Host "  $Message" -ForegroundColor Cyan
    Write-Host "============================================" -ForegroundColor DarkCyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "  [INFO] $Message" -ForegroundColor Gray
}

function Write-OK {
    param([string]$Message)
    Write-Host "  [OK]   $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [WARN] $Message" -ForegroundColor Yellow
}

function Write-Err {
    param([string]$Message)
    Write-Host "  [ERR]  $Message" -ForegroundColor Red
}

# ============================================
# STEP 0: PREPARATION
# ============================================

Write-Host ""
Write-Host "=========================================" -ForegroundColor Magenta
Write-Host "   HANDVISION - RELEASE BUILDER          " -ForegroundColor Magenta
Write-Host "   Embedded Python Distribution          " -ForegroundColor Magenta
Write-Host "=========================================" -ForegroundColor Magenta
Write-Host ""
Write-Info "Project root : $PROJECT_ROOT"
Write-Info "Output dir   : $OUTPUT_DIR"
Write-Info "Python ver   : $PYTHON_VERSION"
Write-Host ""

# Clean if requested
if ($Clean -and (Test-Path $DIST_DIR)) {
    Write-Step "CLEANING DIST FOLDER"
    Remove-Item -Recurse -Force $DIST_DIR
    Write-OK "Cleaned: $DIST_DIR"
}

# Create directories
New-Item -ItemType Directory -Force -Path $OUTPUT_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $TEMP_DIR   | Out-Null

# ============================================
# STEP 1-4: PYTHON SETUP
# ============================================

if (-not $SkipPython) {

    # ---- STEP 1: DOWNLOAD ----
    Write-Step "STEP 1: DOWNLOAD PYTHON $PYTHON_VERSION EMBEDDABLE"

    if (-not (Test-Path $PYTHON_ZIP)) {
        Write-Info "Downloading from $PYTHON_URL ..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $PYTHON_URL -OutFile $PYTHON_ZIP -UseBasicParsing
        Write-OK "Downloaded: $PYTHON_ZIP"
    }
    else {
        Write-Info "Python zip already exists, skipping download"
    }

    # Extract
    if (Test-Path $PYTHON_DIR) {
        Write-Info "Removing existing python dir..."
        Remove-Item -Recurse -Force $PYTHON_DIR
    }
    Write-Info "Extracting to $PYTHON_DIR ..."
    Expand-Archive -Path $PYTHON_ZIP -DestinationPath $PYTHON_DIR -Force
    Write-OK "Extracted Python embeddable package"

    # ---- STEP 2: PATCH ._pth ----
    Write-Step "STEP 2: PATCH PYTHON PATH CONFIG"

    $pthFile = Join-Path $PYTHON_DIR "python${PYTHON_MAJOR_MINOR}._pth"
    if (Test-Path $pthFile) {
        Write-Info "Patching: $pthFile"

        $pthContent = Get-Content $pthFile -Raw

        # Uncomment 'import site'
        $pthContent = $pthContent -replace '#\s*import site', 'import site'

        # Ensure Lib\site-packages is listed
        if ($pthContent -notmatch 'Lib\\site-packages') {
            $pthContent = $pthContent.TrimEnd() + "`nLib\site-packages`n"
        }

        Set-Content -Path $pthFile -Value $pthContent -NoNewline
        Write-OK "Patched ._pth file, import site enabled"

        Write-Info "Contents:"
        Get-Content $pthFile | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    else {
        Write-Err "Cannot find $pthFile, manual patching may be needed"
    }

    # ---- STEP 3: BOOTSTRAP PIP ----
    Write-Step "STEP 3: BOOTSTRAP PIP"

    $embeddedPython = Join-Path $PYTHON_DIR "python.exe"

    if (-not (Test-Path $GET_PIP_FILE)) {
        Write-Info "Downloading get-pip.py ..."
        Invoke-WebRequest -Uri $GET_PIP_URL -OutFile $GET_PIP_FILE -UseBasicParsing
        Write-OK "Downloaded: $GET_PIP_FILE"
    }
    else {
        Write-Info "get-pip.py already exists, skipping download"
    }

    Write-Info "Running get-pip.py with embedded Python ..."
    & $embeddedPython $GET_PIP_FILE --no-warn-script-location 2>&1 | ForEach-Object {
        Write-Host "    $_" -ForegroundColor DarkGray
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to bootstrap pip! Exit code: $LASTEXITCODE"
        exit 1
    }
    Write-OK "pip installed successfully"

    # ---- STEP 4: INSTALL PACKAGES ----
    Write-Step "STEP 4: INSTALL PYTHON PACKAGES"

    foreach ($pkg in $PIP_PACKAGES) {
        Write-Info "Installing: $pkg ..."

        & $embeddedPython -m pip install $pkg --no-warn-script-location 2>&1 | ForEach-Object {
            Write-Host "    $_" -ForegroundColor DarkGray
        }

        if ($LASTEXITCODE -ne 0) {
            Write-Err "Failed to install $pkg! Exit code: $LASTEXITCODE"
            exit 1
        }
        Write-OK "Installed: $pkg"
    }

    # Verify installations
    Write-Info "Verifying Python packages ..."
    & $embeddedPython -c "import cv2; import mediapipe; import numpy; print('OK: cv2=' + cv2.__version__ + ' mp=' + mediapipe.__version__ + ' np=' + numpy.__version__)" 2>&1 | ForEach-Object {
        Write-Host "    $_" -ForegroundColor Green
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "Package verification failed!"
        exit 1
    }
    Write-OK "All Python packages verified"

}
else {
    Write-Step "SKIPPING PYTHON SETUP (SkipPython flag set)"
}

# ============================================
# STEP 5: PUBLISH C# WPF
# ============================================

if (-not $SkipDotnet) {
    Write-Step "STEP 5: PUBLISH C# WPF SELF-CONTAINED"

    Write-Info "Running dotnet publish ..."
    & dotnet publish `
        -c Release `
        -r win-x64 `
        --self-contained true `
        -p:PublishSingleFile=false `
        -p:IncludeNativeLibrariesForSelfExtract=true `
        -o $OUTPUT_DIR `
        $PROJECT_ROOT 2>&1 | ForEach-Object {
        Write-Host "    $_" -ForegroundColor DarkGray
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "dotnet publish failed! Exit code: $LASTEXITCODE"
        exit 1
    }
    Write-OK "C# WPF published to $OUTPUT_DIR"

}
else {
    Write-Step "SKIPPING .NET PUBLISH (SkipDotnet flag set)"
}

# ============================================
# STEP 6: COPY ASSETS
# ============================================

Write-Step "STEP 6: COPY ASSETS"

# Copy hand_tracker.py
$scriptSrc = Join-Path $PROJECT_ROOT "hand_tracker.py"
$scriptDst = Join-Path $OUTPUT_DIR "hand_tracker.py"
if (Test-Path $scriptSrc) {
    Copy-Item -Force $scriptSrc $scriptDst
    Write-OK "Copied: hand_tracker.py"
}
else {
    Write-Err "hand_tracker.py not found at $scriptSrc"
    exit 1
}

# Copy hand_landmarker.task
$modelSrc = Join-Path $PROJECT_ROOT "hand_landmarker.task"
$modelDst = Join-Path $OUTPUT_DIR "hand_landmarker.task"
if (Test-Path $modelSrc) {
    Copy-Item -Force $modelSrc $modelDst
    $modelSizeMB = '{0:N1} MB' -f ((Get-Item $modelSrc).Length / 1MB)
    Write-OK "Copied: hand_landmarker.task ($modelSizeMB)"
}
else {
    Write-Err "hand_landmarker.task not found at $modelSrc"
    exit 1
}

# ============================================
# STEP 7: CLEANUP
# ============================================

Write-Step "STEP 7: CLEANUP"

# Remove __pycache__ from embedded python to save space
$pycacheDirs = Get-ChildItem -Recurse -Directory -Filter "__pycache__" -Path $PYTHON_DIR -ErrorAction SilentlyContinue
foreach ($d in $pycacheDirs) {
    Remove-Item -Recurse -Force $d.FullName -ErrorAction SilentlyContinue
}
Write-OK "Cleaned __pycache__ directories"

# Remove temp dir
if (Test-Path $TEMP_DIR) {
    Remove-Item -Recurse -Force $TEMP_DIR
    Write-OK "Cleaned temp directory"
}

# ============================================
# SUMMARY
# ============================================

Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "   BUILD COMPLETE!                       " -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host ""

# Calculate size
$totalSize = (Get-ChildItem -Recurse $OUTPUT_DIR | Measure-Object -Property Length -Sum).Sum
Write-Info "Output: $OUTPUT_DIR"
$totalMB = '{0:N0} MB' -f ($totalSize / 1MB)
Write-Info "Total size: $totalMB"
Write-Host ""

# List key files
Write-Info "Key files:"
$keyFiles = @(
    "HandVision.exe",
    "hand_tracker.py",
    "hand_landmarker.task",
    "python\python.exe"
)
foreach ($f in $keyFiles) {
    $fPath = Join-Path $OUTPUT_DIR $f
    if (Test-Path $fPath) {
        $fKB = '{0:N0} KB' -f ((Get-Item $fPath).Length / 1KB)
        Write-Host "    [OK] $f ($fKB)" -ForegroundColor Green
    }
    else {
        Write-Host "    [X]  $f (MISSING)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  To run: dist\HandVision\HandVision.exe" -ForegroundColor Yellow
Write-Host ""
