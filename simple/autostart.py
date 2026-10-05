"""Автозапуск при входе в Windows — только средствами самой программы.

Используется самый «прозрачный» для пользователя механизм: ярлык в папке
**Автозагрузка** текущего пользователя (``%APPDATA%\\Microsoft\\Windows\\Start
Menu\\Programs\\Startup``).  Реестр не трогается вообще, права администратора не
нужны, а включить или выключить автозапуск можно одной галочкой в настройках:
она создаёт или удаляет один файл, который видно в Проводнике и в Диспетчере
задач → «Автозагрузка».

Ярлык создаётся встроенным в Windows PowerShell (COM ``WScript.Shell``), поэтому
сторонние библиотеки не требуются.  Если PowerShell почему-то недоступен,
используется запасной вариант — маленький ``.cmd``-файл в той же папке: он
запускает программу без окна консоли (через ``pythonw.exe``) или сам exe.

При автозапуске программа стартует свёрнутой в трей (аргумент ``--tray``), чтобы
вход в систему не открывал окно поверх остальных.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Tuple
from app.i18n import t


#: Имена файлов в папке «Автозагрузка».  Уникальны и узнаваемы: ``disable()``
#: удаляет только их и никогда не тронет чужие ярлыки.
SHORTCUT_NAME = "Xray Config Checker Simple.lnk"
COMMAND_NAME = "Xray Config Checker Simple.cmd"
ICON_NAME = "ico.ico"

#: Аргумент запуска «свёрнутым в трей» (см. ``simple/cli.py``).
TRAY_ARG = "--tray"


def supported() -> bool:
    """Поддерживается ли автозапуск в этой системе (сейчас — только Windows)."""
    return sys.platform.startswith("win")


def startup_dir() -> str:
    """Папка «Автозагрузка» текущего пользователя; ``""``, если её нет."""
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return ""
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def shortcut_path(directory: Optional[str] = None) -> str:
    """Путь к нашему ярлыку (в тестах каталог подменяется)."""
    base = startup_dir() if directory is None else directory
    return os.path.join(base, SHORTCUT_NAME) if base else ""


def command_path(directory: Optional[str] = None) -> str:
    """Путь к запасному ``.cmd``-файлу автозапуска."""
    base = startup_dir() if directory is None else directory
    return os.path.join(base, COMMAND_NAME) if base else ""


def _project_dir() -> str:
    """Корень проекта (папка, где лежит ``simple_main.py``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _icon_for(base: str, fallback: str) -> str:
    candidate = os.path.join(base, ICON_NAME)
    return candidate if os.path.isfile(candidate) else fallback


def launch_command(start_hidden: bool = True) -> Tuple[str, str, str, str]:
    """Что именно запускать при входе в систему.

    Возвращает ``(исполняемый_файл, аргументы, рабочий_каталог, иконка)``.
    Собранная одним файлом программа запускается напрямую, а из исходников —
    через ``pythonw.exe``: он не показывает окно консоли.
    """
    hidden = f" {TRAY_ARG}" if start_hidden else ""
    if getattr(sys, "frozen", False):
        target = os.path.abspath(sys.executable)
        workdir = os.path.dirname(target)
        return target, hidden.strip(), workdir, _icon_for(workdir, target)

    script = os.path.join(_project_dir(), "simple_main.py")
    python = sys.executable
    windowed = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(windowed):
        python = windowed
    arguments = f'"{script}"{hidden}'
    return python, arguments, os.path.dirname(script), _icon_for(_project_dir(),
                                                                python)


def is_enabled(directory: Optional[str] = None) -> bool:
    """Есть ли наша запись в автозагрузке (ярлык или запасной ``.cmd``)."""
    for path in (shortcut_path(directory), command_path(directory)):
        if path and os.path.isfile(path):
            return True
    return False


def describe(directory: Optional[str] = None) -> str:
    """Короткое описание состояния — для журнала и настроек."""
    if not supported():
        return t("автозапуск поддерживается только в Windows")
    shortcut = shortcut_path(directory)
    if shortcut and os.path.isfile(shortcut):
        return t('включён (ярлык: {})').format(shortcut)
    command = command_path(directory)
    if command and os.path.isfile(command):
        return t('включён (команда: {})').format(command)
    return t("выключен")



def _ps_quote(text: str) -> str:
    """Строковый литерал для PowerShell (одинарные кавычки удваиваются)."""
    return "'" + str(text).replace("'", "''") + "'"


def _shortcut_script(path: str, target: str, arguments: str, workdir: str,
                     icon: str) -> str:
    """Команда PowerShell, создающая ярлык ``path``."""
    return (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"{_ps_quote(path)});"
        f"$s.TargetPath = {_ps_quote(target)};"
        f"$s.Arguments = {_ps_quote(arguments)};"
        f"$s.WorkingDirectory = {_ps_quote(workdir)};"
        f"$s.IconLocation = {_ps_quote(icon)};"
        "$s.Description = 'Xray Config Checker Simple';"
        "$s.WindowStyle = 7;"
        "$s.Save()"
    )


def _run_powershell(script: str) -> Tuple[bool, str]:
    """Выполнить команду PowerShell без мелькания окна консоли."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=30, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        return False, message or t('powershell вернул код {}').format(completed.returncode)
    return True, ""


def _write_command_file(path: str, target: str, arguments: str) -> None:
    """Запасной вариант: ``.cmd`` в папке «Автозагрузка»."""
    line = f'start "" "{target}" {arguments}'.rstrip() + "\r\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("@echo off\r\n")
        handle.write(t("rem Автозапуск Xray Config Checker Simple "
                     "(создано программой)\r\n"))
        handle.write(line)


def enable(directory: Optional[str] = None, start_hidden: bool = True
           ) -> Tuple[bool, str]:
    """Включить автозапуск.  Возвращает ``(успех, сообщение)``."""
    if not supported():
        return False, t("автозапуск поддерживается только в Windows")
    base = startup_dir() if directory is None else directory
    if not base:
        return False, t("не найден каталог автозагрузки пользователя")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as exc:
        return False, t('каталог автозагрузки недоступен: {}').format(exc)

    target, arguments, workdir, icon = launch_command(start_hidden)
    shortcut = shortcut_path(base)
    command = command_path(base)
    ok, error = _run_powershell(
        _shortcut_script(shortcut, target, arguments, workdir, icon))
    if ok:
        if command and os.path.isfile(command):
            # Раньше автозапуск включался запасным способом — убираем за собой.
            try:
                os.remove(command)
            except OSError:
                pass
        return True, t('автозапуск включён (ярлык: {})').format(shortcut)

    try:
        _write_command_file(command, target, arguments)
    except OSError as exc:
        return False, t('не удалось создать запись автозапуска: {}').format(error or exc)
    return True, (t('автозапуск включён запасным способом (файл: {}); ярлык создать не удалось: {}').format(command, error))


def disable(directory: Optional[str] = None) -> Tuple[bool, str]:
    """Выключить автозапуск (удаляется только наша запись)."""
    if not supported():
        return False, t("автозапуск поддерживается только в Windows")
    removed = []
    for path in (shortcut_path(directory), command_path(directory)):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                return False, t('не удалось удалить {}: {}').format(path, exc)
    if not removed:
        return True, t("автозапуск и так выключен")
    return True, t("автозапуск выключен")


def sync(enabled: bool, directory: Optional[str] = None,
         start_hidden: bool = True) -> Tuple[bool, str]:
    """Привести автозапуск в соответствие с галочкой в настройках."""
    return enable(directory, start_hidden) if enabled else disable(directory)
