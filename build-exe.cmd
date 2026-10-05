@echo off
rem Пересборка exe simple-версии.
rem
rem     build-exe.cmd          — быстрая пересборка по кэшу PyInstaller
rem                              (обычный случай: изменился код или ico.ico)
rem     build-exe.cmd clean    — полная пересборка с нуля (--clean)
rem
rem Иконка берётся из ico.ico (она же встраивается в exe, см. spec-файл).
rem Если иконка в проводнике не обновилась — это кэш Windows:
rem     ie4uinit.exe -show
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set EXTRA=
if /i "%~1"=="clean" set EXTRA=--clean

echo Сборка XrayConfigCheckerSimple.exe %EXTRA% ...
python -m PyInstaller XrayConfigCheckerSimple.spec --noconfirm %EXTRA%
if errorlevel 1 goto error

echo.
echo Готово: dist\XrayConfigCheckerSimple.exe
for %%F in ("dist\XrayConfigCheckerSimple.exe") do echo Размер: %%~zF байт, изменён: %%~tF
echo Если проводник показывает старую иконку, выполните: ie4uinit.exe -show
exit /b 0

:error
echo.
echo Ошибка сборки — смотрите вывод PyInstaller выше.
exit /b 1
