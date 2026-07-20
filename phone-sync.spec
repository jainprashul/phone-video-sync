# PyInstaller spec for a standalone phone-sync executable.
# Build: pyinstaller phone-sync.spec

block_cipher = None

a = Analysis(
    ["src/phone_video_sync/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        "phone_video_sync.cli",
        "phone_video_sync.pipeline.core",
        "phone_video_sync.adb.client",
        "phone_video_sync.report",
        "typer",
        "rich",
        "questionary",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="phone-sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
