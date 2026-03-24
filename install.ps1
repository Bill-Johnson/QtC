# QtC Windows Installer
# Run from PowerShell as your normal user (no admin required for user install)
# Usage: Right-click install.ps1 -> "Run with PowerShell"
#   or:  powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$VERSION = "0.9.10-beta"
$INSTALL_DIR = "$env:APPDATA\qtc"
$BACKUP_CONFIG = "$env:USERPROFILE\qtc_config_backup.json"
$BACKUP_DB     = "$env:USERPROFILE\qtc_messages_backup.db"

Write-Host ""
Write-Host "  QtC $VERSION - Windows Installer" -ForegroundColor Cyan
Write-Host "  Install directory: $INSTALL_DIR"
Write-Host ""

# --- 1. Check Python ---
Write-Host "Checking Python..." -NoNewline
try {
    $pyver = python --version 2>&1
    Write-Host " $pyver" -ForegroundColor Green
} catch {
    Write-Host " NOT FOUND" -ForegroundColor Red
    Write-Host ""
    Write-Host "Python 3.10 or newer is required." -ForegroundColor Yellow
    Write-Host "Download from: https://www.python.org/downloads/"
    Write-Host "Make sure to check 'Add Python to PATH' during installation."
    Read-Host "Press Enter to exit"
    exit 1
}

$pyvernum = (python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1) | Select-Object -First 1
if ([version]$pyvernum -lt [version]"3.10") {
    Write-Host "Python 3.10 or newer required (found $pyvernum)." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# --- 2. Check / install PyQt6 and pyserial ---
Write-Host "Checking PyQt6..." -NoNewline
try {
    $null = python -c "import PyQt6" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " installing..." -ForegroundColor Yellow
        python -m pip install --user PyQt6
    }
} catch {
    Write-Host " installing..." -ForegroundColor Yellow
    python -m pip install --user PyQt6
}

Write-Host "Checking pyserial..." -NoNewline
try {
    $null = python -c "import serial" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " installing..." -ForegroundColor Yellow
        python -m pip install --user pyserial
    }
} catch {
    Write-Host " installing..." -ForegroundColor Yellow
    python -m pip install --user pyserial
}

# --- 3. Verify all source files match version ---
Write-Host "Verifying version headers..."
$files = @("main_window.py","bbs_session.py","transport.py","database.py","ptt.py")
foreach ($f in $files) {
    if (-not (Test-Path $f)) {
        Write-Host "  ERROR: $f not found in current directory." -ForegroundColor Red
        Write-Host "  Run this installer from the QtC-$VERSION folder."
        Read-Host "Press Enter to exit"
        exit 1
    }
    $line1 = Get-Content $f -TotalCount 1 -Encoding UTF8
    if ($line1 -notmatch "QtC v$VERSION") {
        Write-Host "  ERROR: $f version mismatch (got: $line1)" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "  OK: $f" -ForegroundColor Green
}

# --- 4. Backup existing install ---
if (Test-Path "$INSTALL_DIR\config.json") {
    Copy-Item "$INSTALL_DIR\config.json" $BACKUP_CONFIG -Force
    Write-Host "  Config backed up to $BACKUP_CONFIG"
}
if (Test-Path "$INSTALL_DIR\data\messages.db") {
    Copy-Item "$INSTALL_DIR\data\messages.db" $BACKUP_DB -Force
    Write-Host "  Database backed up to $BACKUP_DB"
}

# --- 5. Install files ---
Write-Host "Installing to $INSTALL_DIR ..."
New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null
foreach ($f in $files) {
    Copy-Item $f "$INSTALL_DIR\$f" -Force
}
if (Test-Path "qtc_icon.svg") {
    Copy-Item "qtc_icon.svg" "$INSTALL_DIR\qtc_icon.svg" -Force
}
if (Test-Path "qtc_icon.ico") {
    Copy-Item "qtc_icon.ico" "$INSTALL_DIR\qtc_icon.ico" -Force
}
if (Test-Path "README.txt") {
    Copy-Item "README.txt" "$INSTALL_DIR\README.txt" -Force
}
if (Test-Path "README.md") {
    Copy-Item "README.md" "$INSTALL_DIR\README.md" -Force
}
if (Test-Path "LICENSE") {
    Copy-Item "LICENSE" "$INSTALL_DIR\LICENSE" -Force
}
Write-Host "  Files installed." -ForegroundColor Green

# Restore database backup if no existing db
if (-not (Test-Path "$INSTALL_DIR\data\messages.db") -and (Test-Path $BACKUP_DB)) {
    New-Item -ItemType Directory -Force -Path "$INSTALL_DIR\data" | Out-Null
    Copy-Item $BACKUP_DB "$INSTALL_DIR\data\messages.db" -Force
    Write-Host "  Restored database from backup"
}

# Restore config backup
if (-not (Test-Path "$INSTALL_DIR\config.json") -and (Test-Path $BACKUP_CONFIG)) {
    Copy-Item $BACKUP_CONFIG "$INSTALL_DIR\config.json" -Force
    Write-Host "  Restored config from backup"
}

# --- 6. Create launcher batch file ---
$launcherPath = "$INSTALL_DIR\qtc.bat"
@"
@echo off
cd /d "%APPDATA%\qtc"
python main_window.py %*
"@ | Set-Content $launcherPath

# --- 7. Generate icon and create desktop shortcut ---
# Generate .ico from qtc_icon.svg using Python + Pillow
Write-Host "Generating icon..."
$iconScript = @"
from PIL import Image, ImageDraw, ImageFont
import io, os, sys

def make_qtc_icon(size):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = max(1, int(size * 0.03))
    draw.ellipse([margin, margin, size-margin-1, size-margin-1],
                 fill=(26, 42, 26, 255), outline=(58, 90, 58, 255),
                 width=max(1, size//64))
    cx = int(size * 0.67); cy = int(size * 0.50)
    green = (0, 255, 136, 255); gm = (0, 255, 136, 165); gd = (0, 255, 136, 89)
    for r, col, w in [(int(size*0.12),green,max(2,size//20)),
                      (int(size*0.20),gm,max(1,size//28)),
                      (int(size*0.29),gd,max(1,size//40))]:
        draw.arc([cx-r,cy-r,cx+r,cy+r], start=-60, end=60, fill=col, width=w)
    fs = max(6, int(size * 0.28))
    try:
        font = ImageFont.truetype('arialbd.ttf', fs)
    except:
        try:
            font = ImageFont.truetype('arial.ttf', fs)
        except:
            font = ImageFont.load_default()
    bbox = draw.textbbox((0,0), 'QtC', font=font)
    tx = int(size * 0.10); ty = (size-(bbox[3]-bbox[1]))//2 - bbox[1]
    draw.text((tx, ty), 'QtC', fill=green, font=font)
    return img

sizes = [256,128,64,48,32,16]
imgs = [make_qtc_icon(s) for s in sizes]
buf = io.BytesIO()
imgs[0].save(buf, format='ICO', append_images=imgs[1:])
ico_path = os.path.join(os.environ['APPDATA'], 'qtc', 'qtc_icon.ico')
with open(ico_path, 'wb') as f:
    f.write(buf.getvalue())
print('icon OK')
"@
try {
    $result = python -c $iconScript 2>&1
    if ($result -match "icon OK") {
        Write-Host "  Icon generated." -ForegroundColor Green
    } else {
        Write-Host "  Icon generation skipped (Pillow not available)." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Icon generation skipped." -ForegroundColor Yellow
}

$WScriptShell = New-Object -ComObject WScript.Shell
$shortcut = $WScriptShell.CreateShortcut("$env:USERPROFILE\Desktop\QtC.lnk")
$shortcut.TargetPath = "python"
$shortcut.Arguments = "$INSTALL_DIR\main_window.py"
$shortcut.WorkingDirectory = $INSTALL_DIR
$shortcut.Description = "QtC BBS Client v$VERSION"
$icoPath = "$INSTALL_DIR\qtc_icon.ico"
if (Test-Path $icoPath) {
    $shortcut.IconLocation = $icoPath
}
$shortcut.Save()
Write-Host "  Desktop shortcut created." -ForegroundColor Green

# --- 8. Add to PATH for current user ---
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$INSTALL_DIR*") {
    [Environment]::SetEnvironmentVariable("PATH", "$userPath;$INSTALL_DIR", "User")
    Write-Host "  Added $INSTALL_DIR to user PATH."
}

# --- 9. Create default config if none exists ---
if (-not (Test-Path "$INSTALL_DIR\config.json")) {
    Write-Host "  Creating default config.json..."
    python -c @"
import json, os
config = {
    'user': {'callsign': 'NOCALL', 'name': '', 'qth': '', 'zip': '', 'home_bbs': ''},
    'bbs_list': [{'name': 'My BBS', 'callsign': 'NOCALL-1', 'transport': 'vara_hf', 'bw': '500'}],
    'vara': {'hf_host': '127.0.0.1', 'hf_cmd_port': 8300, 'hf_data_port': 8301},
    'ptt': {'mode': 'none', 'port': 'COM3', 'signal': 'rts'},
    'app': {'auto_check_mail': True}
}
path = os.path.join(os.environ['APPDATA'], 'qtc', 'config.json')
with open(path, 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=4)
print('  config.json written OK')
"@
}

Write-Host ""
Write-Host "  QtC $VERSION installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  To run: Double-click the QtC shortcut on your Desktop"
Write-Host ""
Write-Host "  VARA HF must be running before connecting." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to exit"
