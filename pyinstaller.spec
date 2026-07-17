# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

spec_path = Path(globals().get("__file__", "pyinstaller.spec")).resolve()
project_root = spec_path.parent
config_file = project_root / "config" / "default_config.json"
docs_dir = project_root / "docs"

# Limit PySide6 surface to shrink bundle and avoid unnecessary WinRT modules.
hidden_imports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # Hardware dependencies loaded dynamically; keep them bundled.
    "serial",
    "serial.tools.list_ports",
    "nidaqmx",
    "nidaqmx.system",
]


def optional_submodules(package_name: str) -> list[str]:
    if find_spec(package_name) is None:
        return []
    return collect_submodules(package_name)


# Collect optional setuptools dependency trees only when present in the active environment.
hidden_imports.extend(optional_submodules("jaraco"))
hidden_imports.extend(optional_submodules("pkg_resources"))
excluded_modules = [
    # Not used: PyQt5 (we use PySide6)
    "PyQt5",
    # Not used in runtime: Tk, IPython/Jupyter, Matplotlib UIs, pytest
    "tkinter",
    "IPython",
    "ipykernel",
    "ipython_genutils",
    "jupyter",
    "jupyter_core",
    "jupyter_client",
    "jupyter_console",
    "notebook",
    "matplotlib",
    "matplotlib.backends",
    "pytest",
    "pkg_resources._vendor.jaraco.text.show-newlines",
    "pkg_resources._vendor.jaraco.text.strip-prefix",
    "pkg_resources._vendor.jaraco.text.to-dvorak",
    "pkg_resources._vendor.jaraco.text.to-qwerty",
    # PySide6 heavy/unused modules
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPositioning",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.plugins.platforms.qdirect2d",
]

block_cipher = None

datas = [
    (str(config_file), "config"),
    (str(docs_dir), "docs"),
]
datas += copy_metadata("nidaqmx")

a = Analysis(
    ["app/main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="OlfactoryPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OlfactoryPilot",
)
