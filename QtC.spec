# QtC.spec — PyInstaller build spec for QtC Windows exe
# Copyright (C) 2025-2026 Bill Johnson, KC9MTP
#
# Usage (run from the directory containing your .py files and qtc_icon.ico):
#   pyinstaller QtC.spec
#
# Output: dist\QtC\QtC.exe  (one-folder build)
# Zip dist\QtC\ and upload as QtC-X.Y.Z-beta-windows.zip to GitHub Releases.

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

a = Analysis(
    ['main_window.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # Bundle the SVG icon and splash PNG alongside the exe
        ('qtc_icon.svg', '.'),
        ('qtc_splash.png', '.'),
    ],
    hiddenimports=[
        # pyserial is imported lazily inside ptt.py functions — must be explicit
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'serial.tools.list_ports_windows',
        # PyQt6 platform plugin — required for windowed exe
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # Our own modules (co-located, not packages)
        'bbs_session',
        'transport',
        'database',
        'ptt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim unused heavy modules to keep exe size down
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'email',
        'html',
        'http',
        'urllib',
        'xml',
        'xmlrpc',
        'test',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Bootloader splash intentionally NOT used — it flashes briefly, then
# disappears for ~1-2s while Python boots, then the Qt QSplashScreen
# appears. That double-flash is more jarring than a single Qt splash
# that comes up once and stays until MainWindow is ready. See
# main_window.py:main() for the Qt splash logic.

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='QtC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX can trigger AV false positives — leave off
    console=False,           # no console window (GUI app)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='qtc_icon.ico',     # requires qtc_icon.ico in same directory
    version_info=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='QtC',              # output folder: dist\QtC\
)
