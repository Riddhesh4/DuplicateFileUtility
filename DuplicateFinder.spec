# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd().resolve()
icon_path = project_root / "assets" / "app.ico"
version_file = project_root / "build_assets" / "version_info.txt"
manifest_file = project_root / "build_assets" / "app.manifest"

a = Analysis(
    ['app_launcher.py'],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        ('build_assets/signing-placeholder.txt', 'build_assets'),
    ],
    hiddenimports=[
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'imagehash',
        'xxhash',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DuplicateFinder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
    version=str(version_file),
    manifest=str(manifest_file),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DuplicateFinder',
)
