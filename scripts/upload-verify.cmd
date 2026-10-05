@echo off
rem ===========================================================================
rem  upload-verify.cmd — залить файл на сервер по SSH, проверить целостность и
rem  атомарно подменить целевой файл (клиент никогда не скачает половину).
rem
rem  Использование во вкладке «Внешние команды» (тип «Готовый скрипт»):
rem      Аргументы:   "{file}" {host} {port} {user} {remote_dir}
rem      Пароль:      поле «Пароль/токен» пресета (скрипт берёт COMMAND_PASSWORD)
rem
rem  Транспорт выбирается автоматически:
rem      1) pscp/plink (PuTTY) — понимают пароль: -pw "%COMMAND_PASSWORD%";
rem      2) scp/ssh (OpenSSH)  — только вход по ключу: пароль вводить нельзя.
rem
rem  Порядок работы:
rem      1. файл заливается рядом с целью под именем working.tmp;
rem      2. сравнивается sha256 (а если на сервере нет sha256sum — размер)
rem         локального файла и загруженного;
rem      3. при совпадении выполняется mv working.tmp working.txt, иначе
rem         временный файл на сервере удаляется и скрипт ругается.
rem
rem  Коды возврата: 0 — загружено и проверено, 1 — ошибка, 2 — аргументы.
rem  Самотест без сети:  upload-verify.cmd --selftest
rem ===========================================================================
setlocal EnableExtensions
set "REMOTE_NAME=working.txt"
set "REMOTE_TMP=working.tmp"

if /i "%~1"=="--selftest" goto :selftest

set "FILE=%~1"
set "HOST=%~2"
set "PORT=%~3"
set "USER=%~4"
set "RDIR=%~5"
if "%PORT%"=="" set "PORT=22"
if "%RDIR%"=="" set "RDIR=."
if "%FILE%"=="" goto :usage
if "%HOST%"=="" goto :usage
if "%USER%"=="" goto :usage
if not exist "%FILE%" goto :missing_file

rem -- инструменты -----------------------------------------------------------
set "PSCP=pscp"
set "PLINK=plink"
if exist "%ProgramFiles%\PuTTY\pscp.exe" set "PSCP=%ProgramFiles%\PuTTY\pscp.exe"
if exist "%ProgramFiles%\PuTTY\plink.exe" set "PLINK=%ProgramFiles%\PuTTY\plink.exe"
set "SCP=scp"
set "SSH=ssh"
if exist "%SystemRoot%\System32\OpenSSH\scp.exe" set "SCP=%SystemRoot%\System32\OpenSSH\scp.exe"
if exist "%SystemRoot%\System32\OpenSSH\ssh.exe" set "SSH=%SystemRoot%\System32\OpenSSH\ssh.exe"

set "TRANSPORT=scp"
if not "%COMMAND_PASSWORD%"=="" set "TRANSPORT=pscp"

set "REMOTE=%USER%@%HOST%"
set "STAGE=%RDIR%/%REMOTE_TMP%"
set "TARGET=%RDIR%/%REMOTE_NAME%"

if "%TRANSPORT%"=="pscp" goto :tools_pscp
set COPY="%SCP%" -P %PORT% -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
set SSHTOOL="%SSH%" -p %PORT% -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
goto :upload

:tools_pscp
set COPY="%PSCP%" -batch -pw "%COMMAND_PASSWORD%" -P %PORT%
set SSHTOOL="%PLINK%" -batch -pw "%COMMAND_PASSWORD%" -P %PORT%

rem -- 1. загрузка во временный файл -------------------------------------------
:upload
echo [upload-verify] загрузка "%FILE%" на %REMOTE%:%STAGE% (порт %PORT%, транспорт %TRANSPORT%)
%COPY% "%FILE%" "%REMOTE%:%STAGE%"
if errorlevel 1 goto :fail_upload

rem -- 2. проверка целостности --------------------------------------------------
call :hash_local "%FILE%"
set "LOCAL_HASH=%HASH%"
for %%F in ("%FILE%") do set "LOCAL_SIZE=%%~zF"

set "REMOTE_HASH="
for /f "usebackq delims=" %%H in (`%SSHTOOL% %REMOTE% "sha256sum '%STAGE%' 2>/dev/null | cut -d' ' -f1"`) do set "REMOTE_HASH=%%H"
if "%REMOTE_HASH%"=="" goto :verify_by_size
set "REMOTE_HASH=%REMOTE_HASH: =%"
if /i not "%REMOTE_HASH%"=="%LOCAL_HASH%" goto :fail_hash
echo [upload-verify] файл на сервере совпал по sha256: %LOCAL_HASH%
goto :replace

:verify_by_size
set "REMOTE_SIZE="
for /f "usebackq delims=" %%S in (`%SSHTOOL% %REMOTE% "wc -c < '%STAGE%'"`) do set "REMOTE_SIZE=%%S"
if "%REMOTE_SIZE%"=="" goto :fail_verify
if not "%REMOTE_SIZE%"=="%LOCAL_SIZE%" goto :fail_size
echo [upload-verify] sha256sum на сервере недоступен, совпал размер: %LOCAL_SIZE% байт

rem -- 3. атомарная замена -----------------------------------------------------
:replace
%SSHTOOL% %REMOTE% "mv -f '%STAGE%' '%TARGET%'"
if errorlevel 1 goto :fail_replace
echo [upload-verify] готово: обновлён %REMOTE%:%TARGET% (%LOCAL_SIZE% байт)
exit /b 0

:fail_upload
echo [upload-verify] ОШИБКА: загрузка не удалась (проверьте адрес, порт, пароль и ключ хоста)
exit /b 1

:fail_hash
echo [upload-verify] ОШИБКА: sha256 не совпал (локально %LOCAL_HASH%, на сервере %REMOTE_HASH%)
%SSHTOOL% %REMOTE% "rm -f '%STAGE%'" >nul 2>nul
exit /b 1

:fail_size
echo [upload-verify] ОШИБКА: размер не совпал (локально %LOCAL_SIZE%, на сервере %REMOTE_SIZE%)
%SSHTOOL% %REMOTE% "rm -f '%STAGE%'" >nul 2>nul
exit /b 1

:fail_verify
echo [upload-verify] ОШИБКА: не удалось узнать размер файла на сервере
%SSHTOOL% %REMOTE% "rm -f '%STAGE%'" >nul 2>nul
exit /b 1

:fail_replace
echo [upload-verify] ОШИБКА: файл загружен и проверен, но mv не сработал
exit /b 1

:missing_file
echo [upload-verify] ОШИБКА: файл не найден: %FILE%
exit /b 1

:usage
echo [upload-verify] использование: upload-verify.cmd "локальный файл" хост порт пользователь "удалённый каталог"
exit /b 2

rem -- локальный sha256 (первая строка без пробелов после заголовка) -----------
:hash_local
set "HASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%~1" SHA256 2^>nul') do call :take_hash "%%H"
exit /b 0

:take_hash
if defined HASH exit /b 0
set "LINE=%~1"
if "%LINE%"=="" exit /b 0
set "HASH=%LINE: =%"
exit /b 0

rem -- самотест логики без сети: хеши файла и его копии должны совпасть --------
:selftest
set "S1=%TEMP%\upload_verify_a%RANDOM%.txt"
set "S2=%TEMP%\upload_verify_b%RANDOM%.txt"
echo upload-verify selftest> "%S1%"
copy /y "%S1%" "%S2%" >nul
call :hash_local "%S1%"
set "H1=%HASH%"
call :hash_local "%S2%"
set "H2=%HASH%"
del "%S1%" "%S2%" >nul 2>nul
if "%H1%"=="" (echo [upload-verify] самотест: НЕ УДАЛОСЬ получить хеш& exit /b 1)
if /i not "%H1%"=="%H2%" (echo [upload-verify] самотест: хеши разошлись& exit /b 1)
echo [upload-verify] самотест пройден: sha256 файла и копии совпали (%H1%)
exit /b 0
