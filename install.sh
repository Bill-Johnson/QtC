#!/usr/bin/env bash
# QtC — Install Script
# Installs QtC BBS Client on Linux (Fedora, Ubuntu/Debian, Arch)
# Run as your normal user — sudo will be invoked only where needed.

set -e

APP_NAME="QtC"
INSTALL_DIR="$HOME/.local/share/qtc"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================"
echo "  QtC BBS Client — Installer"
echo "============================================"
echo ""

# ── Detect distro and install system dependencies ─────────────────
install_system_deps() {
    echo "→ Checking system dependencies..."

    if command -v python3 &>/dev/null && python3 -c "import PyQt6" &>/dev/null 2>&1; then
        echo "  PyQt6 already available system-wide."
        return
    fi

    if command -v dnf &>/dev/null; then
        echo "  Detected Fedora/RHEL — installing via dnf..."
        sudo dnf install -y python3 python3-pip python3-pyqt6 python3-pyserial 2>/dev/null || true
    elif command -v apt-get &>/dev/null; then
        echo "  Detected Debian/Ubuntu — installing via apt..."
        sudo apt-get update -qq
        sudo apt-get install -y python3 python3-pip python3-pyqt6 python3-serial 2>/dev/null || true
    elif command -v pacman &>/dev/null; then
        echo "  Detected Arch — installing via pacman..."
        sudo pacman -S --noconfirm python python-pyqt6 python-pyserial 2>/dev/null || true
    else
        echo "  Unknown distro — will try pip install."
    fi
}

# ── Install Python dependencies via pip if needed ─────────────────
install_pip_deps() {
    echo "→ Checking Python package dependencies..."
    if ! python3 -c "import PyQt6" &>/dev/null 2>&1; then
        echo "  Installing PyQt6 via pip..."
        pip3 install --user PyQt6 pyserial
    fi
    if ! python3 -c "import serial" &>/dev/null 2>&1; then
        echo "  Installing pyserial via pip..."
        pip3 install --user pyserial
    fi
    echo "  Dependencies OK."
}

# ── Copy application files ─────────────────────────────────────────
install_app_files() {
    echo "→ Installing QtC to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    cp "$SCRIPT_DIR"/*.py "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/README.md" "$INSTALL_DIR/"
    if [ -f "$SCRIPT_DIR/README.txt" ]; then
        cp "$SCRIPT_DIR/README.txt" "$INSTALL_DIR/"
    fi
    if [ -f "$SCRIPT_DIR/LICENSE" ]; then
        cp "$SCRIPT_DIR/LICENSE" "$INSTALL_DIR/"
    fi
    # Copy icon
    if [ -f "$SCRIPT_DIR/qtc_icon.svg" ]; then
        cp "$SCRIPT_DIR/qtc_icon.svg" "$INSTALL_DIR/"
    fi
    # Don't overwrite existing config — preserve user settings
    # Check for existing config, then backup from previous uninstall, then default
    if [ -f "$INSTALL_DIR/config.json" ]; then
        echo "  Kept existing config.json (user settings preserved)"
    elif [ -f "$HOME/qtc_config_backup.json" ]; then
        cp "$HOME/qtc_config_backup.json" "$INSTALL_DIR/config.json"
        echo "  Restored config.json from backup (~/qtc_config_backup.json)"
    else
        cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/"
        echo "  Created default config.json"
    fi
    echo "  Files installed."
    # Restore database backup if present (preserves inbox, address book, sent)
    if [ ! -f "$INSTALL_DIR/data/messages.db" ] && [ -f "$HOME/qtc_messages_backup.db" ]; then
        mkdir -p "$INSTALL_DIR/data"
        cp "$HOME/qtc_messages_backup.db" "$INSTALL_DIR/data/messages.db"
        echo "  Restored database from backup (inbox, address book, sent messages)"
    fi
    # Verify all files carry matching version headers
    echo "  Verifying file versions..."
    mismatch=0
    for f in main_window.py bbs_session.py transport.py database.py ptt.py; do
        ver=$(head -1 "$INSTALL_DIR/$f" 2>/dev/null | grep -o "v[0-9.a-z-]*" | head -1)
        if [ -z "$ver" ]; then
            echo "  WARNING: $f has no version header"
            mismatch=1
        fi
    done
    # Check they all match
    vers=$(for f in main_window.py bbs_session.py transport.py database.py ptt.py; do
        head -1 "$INSTALL_DIR/$f" 2>/dev/null | grep -o "v[0-9.a-z-]*" | head -1
    done | sort -u | wc -l)
    if [ "$vers" -gt 1 ]; then
        echo "  WARNING: Version mismatch detected across files!"
        for f in main_window.py bbs_session.py transport.py database.py ptt.py; do
            echo "    $(head -1 "$INSTALL_DIR/$f" 2>/dev/null)"
        done
    else
        ver=$(head -1 "$INSTALL_DIR/main_window.py" | grep -o "v[0-9.a-z-]*" | head -1)
        echo "  All files verified: $ver"
    fi
}

# ── Create launcher script ─────────────────────────────────────────
install_launcher() {
    echo "→ Creating qtc launcher in $BIN_DIR..."
    mkdir -p "$BIN_DIR"
    cat > "$BIN_DIR/qtc" << LAUNCHER
#!/usr/bin/env bash
# QtC BBS Client launcher
cd "$INSTALL_DIR"
exec python3 "$INSTALL_DIR/main_window.py" "\$@"
LAUNCHER
    chmod +x "$BIN_DIR/qtc"
    echo "  Launcher created: $BIN_DIR/qtc"
}

# ── Install desktop entry and icon ────────────────────────────────
install_desktop_entry() {
    echo "→ Installing desktop entry and icon..."
    mkdir -p "$DESKTOP_DIR"
    mkdir -p "$ICON_DIR"

    # Install SVG icon
    if [ -f "$INSTALL_DIR/qtc_icon.svg" ]; then
        cp "$INSTALL_DIR/qtc_icon.svg" "$ICON_DIR/qtc.svg"
    fi

    # Create .desktop file
    cat > "$DESKTOP_DIR/qtc.desktop" << DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=QtC BBS Client
GenericName=Packet BBS Client
Comment=Amateur Radio BBS client for LinBPQ via VARA HF/FM and Telnet
Exec=$BIN_DIR/qtc
Icon=qtc
Terminal=false
Categories=HamRadio;Network;
Keywords=ham;radio;bbs;vara;packet;linbpq;
StartupNotify=true
DESKTOP

    # Refresh desktop database
    if command -v update-desktop-database &>/dev/null; then
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache &>/dev/null; then
        gtk-update-icon-cache -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    fi
    echo "  Desktop entry installed."
}

# ── Check PATH ────────────────────────────────────────────────────
check_path() {
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        echo ""
        echo "  NOTE: $BIN_DIR is not in your PATH."
        echo "  Add this line to your ~/.bashrc or ~/.bash_profile:"
        echo ""
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo ""
        echo "  Then run:  source ~/.bashrc"
        echo "  Or just start a new terminal session."
    fi
}

# ── Run installer ─────────────────────────────────────────────────
install_system_deps
install_pip_deps
install_app_files
install_launcher
install_desktop_entry
check_path

echo ""
echo "============================================"
echo "  QtC installed successfully!"
echo ""
echo "  Start from terminal:  qtc"
echo "  Or find QtC in your applications menu."
echo "============================================"
echo ""
