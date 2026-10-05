# -*- mode: python ; coding: utf-8 -*-
"""Сборка монолитного exe без консоли: XrayConfigCheckerSimple.exe.

    python -m PyInstaller XrayConfigCheckerSimple.spec --noconfirm --clean

Что попадает внутрь: только код (``app/`` — ядро проверки и ``simple/`` —
надстройка с окном), иконка ``ico.ico``, стандартная библиотека Python и
Tkinter.  Никаких данных: ``data/``, ``logs/``, ``tmp/``, ``core/``, тесты,
``.git`` и файлы настроек с серверами и паролями в сборку не включаются —
иконка при этом упакована, чтобы трей находил её и в распакованном виде, а сами
файлы данных приложение создаёт заново рядом с exe при первом запуске.

Список исключений снимает лишнее: тесты и неиспользуемые модули стандартной
библиотеки (SQLite в этой версии не нужен — пул живёт в ``pool.json``).
``optimize=2`` убирает docstring-и и assert-ы из байткода, ``upx=False`` — чтобы
сборка не зависела от внешнего упаковщика (и не ловила ложные срабатывания
антивирусов).
"""


a = Analysis(
    ['simple_main.py'],
    pathex=[],
    binaries=[],
    datas=[('ico.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['sqlite3', 'unittest', 'pydoc', 'doctest',
              'tests_simple', 'tests_simple_gui'],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [('O', None, 'OPTION'), ('O', None, 'OPTION')],
    name='XrayConfigCheckerSimple',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='ico.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
