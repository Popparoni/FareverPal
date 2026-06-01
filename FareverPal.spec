# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for FareverPal (committed; the single source of build truth).

Replaces the long package.bat CLI so the exact bundle contents are version
controlled and can't drift between machines (the v0.1.3 incident was a build
from uncommitted local state). Build with:

    pyinstaller --noconfirm FareverPal.spec

Key properties:
  - one-file, windowed, branded with assets/app_icon.ico.
  - In-repo assets are always bundled. Sibling game-data dirs from the private
    monorepo (../data/sheets, ../htdocs/assets/...) are added ONLY if present,
    so a local monorepo build ships full data while a bare CI checkout (no
    monorepo) still builds a verification artifact.
  - notes/ is NEVER bundled (it's private/internal).
  - Unused Qt modules are excluded to trim the bundle; only QtCore/QtGui/
    QtWidgets/QtSvg (+ the Windows platform plugin via PySide6's hook) are kept.
"""
import os

from PyInstaller.utils.hooks import collect_dynamic_libs

# --- the native memory reader (built by maturin into the env) --------------
binaries = collect_dynamic_libs("farever_native")

# --- data: in-repo assets (always present) ---------------------------------
datas = [
    ("assets/fonts", "assets/fonts"),
    ("assets/icons_ui", "assets/icons_ui"),
    ("assets/map", "assets/map"),
    ("assets/map_icons", "assets/map_icons"),
    ("assets/app_icon.png", "assets"),
    ("assets/app_icon.ico", "assets"),
]

# --- data: sibling game-data dirs from the private monorepo (optional) ------
# Added only if they exist next to this repo, so local full-data builds include
# the CDB sheets + wiki data + icons, while CI (which checks out only this repo,
# without the monorepo) still builds successfully without them. notes/ is never
# included here.
for src, dst in (
    ("../data/sheets", "data/sheets"),
    ("../htdocs/assets/icons", "htdocs/assets/icons"),
    ("../htdocs/assets/data", "htdocs/assets/data"),
):
    if os.path.exists(src):
        datas.append((src, dst))

# --- excludes: Qt modules the app never uses (trim the bundle) -------------
# Keep only QtCore, QtGui, QtWidgets, QtSvg and the windows platform plugin
# (provided by PySide6's PyInstaller hook). We do NOT use --collect-all, so the
# hook + these excludes are what determine the Qt footprint.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngine",
    "PySide6.QtWebChannel",
    "PySide6.QtWebView",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSql",
    "PySide6.QtWebSockets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtQuickWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtLocation",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSensors",
    "PySide6.QtTextToSpeech",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtStateMachine",
    "PySide6.QtScxml",
]


a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FareverPal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app_icon.ico",
)
