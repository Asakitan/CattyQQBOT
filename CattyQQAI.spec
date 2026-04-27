# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


hiddenimports = [
    'catty_qq_ai',
    'catty_integrations',
    'nonebot.drivers.fastapi',
    'nonebot.adapters.onebot.v11',
]
hiddenimports += collect_submodules('nonebot')
hiddenimports += collect_submodules('nonebot.adapters.onebot')
hiddenimports += collect_submodules('fastapi')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('PIL')
hiddenimports = sorted(set(hiddenimports))

datas = [
    ('src/catty_qq_ai', 'catty_qq_ai'),
]
if os.path.isdir('emojis'):
    datas.append(('emojis', 'emojis'))
datas += collect_data_files('nonebot')
datas += collect_data_files('nonebot.adapters.onebot')
datas += collect_data_files('PIL')


a = Analysis(
    ['bot.py'],
    pathex=['src'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name='CattyQQAI',
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
