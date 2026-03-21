#!/usr/bin/env bash
# QtC — Uninstall Script

INSTALL_DIR="$HOME/.local/share/qtc"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "============================================"
echo "  QtC BBS Client — Uninstaller"
echo "============================================"
echo ""

read -p "Remove QtC? Your config.json will be preserved. (y/N) " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Cancelled."
    exit 0
fi

# Save config and database before removal
if [ -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.json" "$HOME/qtc_config_backup.json"
    echo "→ Config saved to ~/qtc_config_backup.json"
fi
if [ -f "$INSTALL_DIR/data/messages.db" ]; then
    cp "$INSTALL_DIR/data/messages.db" "$HOME/qtc_messages_backup.db"
    echo "→ Database saved to ~/qtc_messages_backup.db"
fi

[ -d "$INSTALL_DIR" ]         && rm -rf "$INSTALL_DIR"         && echo "→ Removed $INSTALL_DIR"
[ -f "$BIN_DIR/qtc" ]         && rm -f  "$BIN_DIR/qtc"         && echo "→ Removed $BIN_DIR/qtc"
[ -f "$DESKTOP_DIR/qtc.desktop" ] && rm -f "$DESKTOP_DIR/qtc.desktop" && echo "→ Removed desktop entry"
[ -f "$ICON_DIR/qtc.svg" ]    && rm -f  "$ICON_DIR/qtc.svg"    && echo "→ Removed icon"

if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

echo ""
echo "QtC removed."
echo "  Config backup:   ~/qtc_config_backup.json"
echo "  Database backup: ~/qtc_messages_backup.db  (inbox, address book, sent)"
echo ""
