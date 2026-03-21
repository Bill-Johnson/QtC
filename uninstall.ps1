# QtC Windows Uninstaller
# Run from PowerShell as your normal user
# Usage: Right-click uninstall.ps1 -> "Run with PowerShell"

$INSTALL_DIR   = "$env:APPDATA\qtc"
$BACKUP_CONFIG = "$env:USERPROFILE\qtc_config_backup.json"
$BACKUP_DB     = "$env:USERPROFILE\qtc_messages_backup.db"
$SHORTCUT      = "$env:USERPROFILE\Desktop\QtC.lnk"

Write-Host ""
Write-Host "  QtC Uninstaller" -ForegroundColor Cyan
Write-Host ""

$confirm = Read-Host "Remove QtC? Your config and messages will be backed up first. (y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") {
    Write-Host "Cancelled."
    exit 0
}

# Backup config and database before removal
if (Test-Path "$INSTALL_DIR\config.json") {
    Copy-Item "$INSTALL_DIR\config.json" $BACKUP_CONFIG -Force
    Write-Host "  Config saved to $BACKUP_CONFIG"
}
if (Test-Path "$INSTALL_DIR\data\messages.db") {
    Copy-Item "$INSTALL_DIR\data\messages.db" $BACKUP_DB -Force
    Write-Host "  Database saved to $BACKUP_DB  (inbox, address book, sent)"
}

# Remove install directory
if (Test-Path $INSTALL_DIR) {
    Remove-Item $INSTALL_DIR -Recurse -Force
    Write-Host "  Removed $INSTALL_DIR"
}

# Remove desktop shortcut
if (Test-Path $SHORTCUT) {
    Remove-Item $SHORTCUT -Force
    Write-Host "  Removed desktop shortcut"
}

# Remove from PATH
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -like "*$INSTALL_DIR*") {
    $newPath = ($userPath -split ";" | Where-Object { $_ -ne $INSTALL_DIR }) -join ";"
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    Write-Host "  Removed from user PATH"
}

Write-Host ""
Write-Host "  QtC removed." -ForegroundColor Green
Write-Host "  Config backup:   $BACKUP_CONFIG"
Write-Host "  Database backup: $BACKUP_DB"
Write-Host ""
Read-Host "Press Enter to exit"
