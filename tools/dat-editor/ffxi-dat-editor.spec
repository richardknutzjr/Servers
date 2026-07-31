# PyInstaller spec for the FFXI DAT editor.
# Build locally on Windows with:
#     pip install -r requirements-build.txt
#     pyinstaller ffxi-dat-editor.spec
# CI does the same on a windows-latest runner (see .github/workflows/build-exe.yml).

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Ship the Flask templates and static assets inside the exe.
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Nothing Tk-based ships with the tool; the exe stays lean.
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
    ],
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
    name='ffxi-dat-editor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True keeps a terminal window open behind the browser. It
    # doubles as the "close to stop the server" affordance for users who
    # don't know what an HTTP daemon is.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
