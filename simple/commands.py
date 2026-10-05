"""Внешние команды: запуск любых скриптов и консольных команд по триггеру.

Раздел намеренно нейтральный — приложение не знает, что именно делает команда:
отправить файл на VPS, в Telegram, в GitHub, на обменник, распаковать, уведомить,
запустить свой bat-файл.  Приложение отвечает только за три вещи:

* **когда** запускать — по изменению выходного файла, по расписанию или вручную;
* **с чем** запускать — подстановка путей к файлам и полей профиля подключения;
* **что получилось** — код возврата, время и вывод (в журнал и в интерфейс).

Такой слой не привязывает проект ни к протоколу, ни к сервису: транспорт
описывается одной строкой команды или готовым скриптом, а не кодом приложения.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.settings import get_logger

from .config import OUTPUT_URI, OUTPUT_V2RAYN, _clean_commands
from app.i18n import t


log = get_logger()

# ---------------------------------------------------------------------------
# Словари для интерфейса
# ---------------------------------------------------------------------------

KIND_LABELS = {
    t("Готовый скрипт"): "script",
    t("Командная строка"): "command",
}
TRIGGER_LABELS = {
    t("При изменении списка"): "on_change",
    t("По расписанию"): "interval",
    t("Только вручную"): "manual",
    t("При запуске автоматизации"): "on_start",
    t("При остановке автоматизации"): "on_stop",
    t("Круг проверок завершён"): "on_round",
    t("Пропала сеть"): "on_network_down",
    t("Сеть восстановилась"): "on_network_up",
}

#: Триггеры-события: срабатывают по отметке от приложения (в отличие от
#: ``interval``, который считает время сам, и ``manual``, который ждёт кнопку).
EVENT_TRIGGERS: Tuple[str, ...] = ("on_change", "on_start", "on_stop",
                                   "on_round", "on_network_down",
                                   "on_network_up")
ARTIFACT_LABELS = {
    t("Список ссылок ({file})"): "links",
    t("Конфиг для v2rayN ({json})"): "json",
    t("Оба файла"): "both",
}

PLACEHOLDERS: Tuple[Tuple[str, str], ...] = (
    ("{file}", t("полный путь к файлу со списком ссылок")),
    ("{json}", t("полный путь к JSON-конфигу для v2rayN")),
    ("{count}", t("сколько рабочих серверов в файле")),
    ("{out_dir}", t("папка, в которой лежат оба файла")),
    ("{name}", t("имя этого пресета")),
    ("{time}", t("время запуска, ЧЧ:ММ:СС")),
    ("{date}", t("дата запуска, ГГГГ-ММ-ДД")),
    ("{host}", t("адрес из профиля подключения")),
    ("{port}", t("порт из профиля подключения")),
    ("{user}", t("пользователь из профиля подключения")),
    ("{password}", t("пароль из профиля подключения")),
    ("{remote_dir}", t("удалённый каталог из профиля подключения")),
)

# Готовые примеры: их можно вставить в новый пресет одной кнопкой и поправить.
EXAMPLES: Tuple[Dict[str, Any], ...] = (
    {
        "title": t("Копировать на VPS по scp (ключ, нестандартный порт)"),
        "note": t("Заливает список ссылок на сервер. Нужен настроенный SSH-ключ "
                "(пароль не потребуется, поэтому включён BatchMode). Порт "
                "берётся из профиля, флаг порта у scp — заглавная -P, у ssh — "
                "строчная -p."),
        "kind": "command",
        "artifact": "links",
        "command": 'scp -P {port} -o BatchMode=yes -o ConnectTimeout=15 "{file}" '
                   "{user}@{host}:{remote_dir}/working.txt",
    },
    {
        "title": t("Залить на VPS по паролю (pscp) и проверить"),
        "note": t("Если вход только по паролю: ssh/scp его не принимают, а pscp "
                "из PuTTY — принимает. Пароль берётся из переменной окружения "
                "COMMAND_PASSWORD (её подставляет приложение), поэтому в "
                "командной строке он не светится. Сначала копия рядом, потом "
                "mv — клиент не скачает половину файла."),
        "kind": "command",
        "artifact": "links",
        "command": 'pscp -batch -pw "%COMMAND_PASSWORD%" -P {port} "{file}" '
                   "{user}@{host}:{remote_dir}/working.tmp && "
                   'plink -batch -pw "%COMMAND_PASSWORD%" -P {port} '
                   "{user}@{host} mv -f '{remote_dir}/working.tmp' "
                   "'{remote_dir}/working.txt'",
    },
    {
        "title": t("Скрипт: загрузка + sha256 + атомарная замена"),
        "note": t("Готовый скрипт из репозитория: сам выбирает pscp (пароль) или "
                "scp (ключ), сравнивает sha256 локального файла и файла на "
                "сервере (или размер, если sha256sum нет) и только потом "
                "подменяет цель. Самотест без сети: --selftest."),
        "kind": "script",
        "artifact": "links",
        "script": "scripts\\upload-verify.cmd",
        "arguments": '"{file}" {host} {port} {user} {remote_dir}',
    },
    {
        "title": t("Залить на VPS и заменить атомарно (ключ)"),
        "note": t("Вариант для входа по ключу: сначала копия рядом, потом "
                "переименование — клиент никогда не скачает половину файла."),
        "kind": "command",
        "artifact": "links",
        "command": 'scp -P {port} -o BatchMode=yes "{file}" {user}@{host}:'
                   "{remote_dir}/working.tmp && ssh -p {port} -o BatchMode=yes "
                   "{user}@{host} mv -f '{remote_dir}/working.tmp' "
                   "'{remote_dir}/working.txt'",
    },
    {
        "title": t("Отправить файл в Telegram-бота"),
        "note": t("Файл придёт как документ в чат. chat_id — ваш личный id или "
                "канал; токен бота удобно держать в поле «пароль». Работает "
                "через локальный прокси v2rayN."),
        "kind": "command",
        "artifact": "links",
        "command": 'curl -sS --fail --max-time 120 '
                   "--proxy socks5h://127.0.0.1:10808 "
                   "-F chat_id={user} -F caption=\"working: {count}\" "
                   '-F document=@"{file}" '
                   '"https://api.telegram.org/bot{password}/sendDocument"',
    },
    {
        "title": t("Создать приватный gist на GitHub"),
        "note": t("Нужен GitHub CLI (`gh auth login` один раз). В выводе команды "
                "будет ссылка на файл — её и указывайте клиенту."),
        "kind": "command",
        "artifact": "links",
        "command": 'gh gist create "{file}" --desc "working {date} {time}"',
    },
    {
        "title": t("Копировать в облако по rclone"),
        "note": t("rclone закрывает SFTP/FTP/WebDAV/S3/Google Drive и держит "
                "реквизиты в своём конфиге — пароль в команду не попадает."),
        "kind": "command",
        "artifact": "both",
        "command": 'rclone copy "{file}" {name}:{remote_dir}/ '
                   '--sftp-host {host} --sftp-user {user} --sftp-port {port}',
    },
    {
        "title": t("Свой bat-файл с аргументами"),
        "note": t("Скрипт получает путь к файлу, адрес и порт — что он делает "
                "дальше, решаете вы. Готовые шаблоны лежат в scripts\\, свои "
                "скрипты можно держать рядом (например, в data\\simple\\scripts "
                "— эта папка не попадает в git)."),
        "kind": "script",
        "artifact": "links",
        "script": "scripts\\upload.cmd",
        "arguments": '"{file}" {host} {port}',
    },
)


# ---------------------------------------------------------------------------
# Пресет
# ---------------------------------------------------------------------------

@dataclass
class CommandPreset:
    """Один пресет внешней команды (хранится в файле настроек)."""

    id: str = ""
    name: str = t("Новая команда")
    kind: str = "script"                 # script | command
    script: str = ""                     # путь к скрипту (kind=script)
    command: str = ""                    # шаблон командной строки (kind=command)
    arguments: str = ""                  # аргументы для скрипта
    artifact: str = "links"              # что передаём: links | json | both
    trigger: str = "on_change"           # on_change | interval | manual
    interval_minutes: float = 60.0
    min_gap_seconds: float = 60.0
    timeout_seconds: float = 600.0
    retry_count: int = 0
    retry_delay_seconds: float = 60.0
    working_dir: str = ""
    host: str = ""
    port: int = 0
    user: str = ""
    password: str = ""
    remote_dir: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    hidden: bool = False                 # не показывать команду и её вывод в GUI

    # Состояние последнего запуска (пишется рантаймом, переживает перезапуск).
    last_run: float = 0.0
    last_status: str = ""
    last_code: Optional[int] = None
    last_duration: float = 0.0
    last_output: str = ""

    # -- сериализация ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CommandPreset":
        if not isinstance(raw, dict):
            return cls()
        cleaned = _clean_commands([raw])
        if cleaned:
            return cls(**cleaned[0])
        return cls(name=str(raw.get("name") or t("Новая команда")))

    def clone(self) -> "CommandPreset":
        """Копия пресета без результатов последнего запуска."""
        data = self.to_dict()
        data["id"] = ""
        data["name"] = t('{} (копия)').format(self.name)
        data["last_run"] = 0.0
        data["last_status"] = ""
        data["last_code"] = None
        data["last_duration"] = 0.0
        data["last_output"] = ""
        return CommandPreset.from_dict(data)

    # -- отображение -----------------------------------------------------
    @property
    def trigger_label(self) -> str:
        for label, value in TRIGGER_LABELS.items():
            if value == self.trigger:
                return label
        return self.trigger

    @property
    def target(self) -> str:
        """Короткое описание того, что запускается (для таблицы)."""
        if self.kind == "script":
            text = self.script or t("(скрипт не указан)")
            if self.arguments:
                text += " " + self.arguments
            return text
        return self.command or t("(команда не указана)")

    @property
    def display_target(self) -> str:
        """То же, но с учётом приватности: скрытый пресет не показывает текст."""
        return t("(скрыто)") if self.hidden else self.target


# ---------------------------------------------------------------------------
# Подстановки
# ---------------------------------------------------------------------------

@dataclass
class RenderedCommand:
    """Результат подстановки: что запускаем и что показываем в журнале."""

    command_line: str = ""
    safe_command_line: str = ""
    warnings: List[str] = field(default_factory=list)


def build_context(preset: CommandPreset, artifacts: Dict[str, Any],
                  when: float) -> Dict[str, str]:
    """Значения для подстановки в шаблон команды."""
    links_path = str(artifacts.get("file") or "")
    json_path = str(artifacts.get("json") or "")
    base_dir = os.path.dirname(links_path or json_path)
    stamp = time.localtime(when)
    return {
        "{file}": links_path,
        "{json}": json_path,
        "{count}": str(artifacts.get("count", 0) or 0),
        "{out_dir}": base_dir,
        "{name}": preset.name,
        "{time}": time.strftime("%H:%M:%S", stamp),
        "{date}": time.strftime("%Y-%m-%d", stamp),
        "{host}": preset.host,
        "{port}": str(preset.port or ""),
        "{user}": preset.user,
        "{password}": preset.password,
        "{remote_dir}": preset.remote_dir,
    }


def mask_secret(text: str, secret: str) -> str:
    """Спрятать секрет: он не должен попадать в журнал и в интерфейс."""
    if not secret:
        return text
    return text.replace(secret, "*" * max(3, min(len(secret), 8)))


def _space_warnings(template: str, context: Dict[str, str]) -> List[str]:
    """Предупредить о незакавыченных путях с пробелами."""
    warnings: List[str] = []
    for key, value in context.items():
        if not value or " " not in value:
            continue
        index = template.find(key)
        if index == -1:
            continue
        before = template[index - 1] if index > 0 else ""
        end = index + len(key)
        after = template[end] if end < len(template) else ""
        if before != '"' or after != '"':
            warnings.append(
                t('значение {} содержит пробелы — возьмите его в кавычки ("{}")').format(key, key))
    return warnings


_SUBSTITUTION_RE = re.compile(r"(?<!\$)\{([A-Za-z0-9_./\\:@+-]*)\}")
_ENV_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")

PROFILE_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("{host}", "host", t("Адрес")),
    ("{port}", "port", t("Порт")),
    ("{user}", "user", t("Пользователь")),
    ("{password}", "password", t("Пароль/токен")),
    ("{remote_dir}", "remote_dir", t("Удалённый каталог")),
)


def _placeholder_warnings(command_line: str,
                          context: Dict[str, str]) -> List[str]:
    """Поймать «самодельные» подстановки вида ``{234234}`` или ``{login}``.

    Ошибка типичная: человек видит ``{file}``/``{port}`` и по аналогии заворачивает
    в фигурные скобки собственные значения.  Приложение такие скобки не трогает,
    поэтому в команду уходит мусор — о нём и предупреждаем.
    """
    unknown: List[str] = []
    for match in _SUBSTITUTION_RE.finditer(command_line):
        name = match.group(1)
        if "{" + name + "}" in context:
            continue                      # это ещё не раскрытая наша подстановка
        if name and name[0].isdigit():
            hint = t(" (значения из профиля подставляются как {host}, {port}, " \
                   "{user}, {password}, {remote_dir})")
        else:
            hint = ""
        unknown.append(match.group(0) + hint)
    if not unknown:
        return []
    return [t("неизвестные подстановки: ") + "; ".join(sorted(set(unknown)))]


def _env_warnings(command_line: str, preset: CommandPreset) -> List[str]:
    """Проверить переменные окружения ``%ИМЯ%``, которые раскрывает cmd.

    Классическая ошибка — вписать сам пароль как ``%mypassword%``: для cmd это
    имя переменной, которой нет, поэтому подставится пустая строка.
    """
    warnings: List[str] = []
    for name in sorted(set(_ENV_RE.findall(command_line))):
        if name == "COMMAND_PASSWORD" or name in preset.env:
            continue
        if name in os.environ:
            continue
        warnings.append(
            t('%{}% — такой переменной окружения нет, cmd подставит пустое значение. Пароль берите из %COMMAND_PASSWORD% (поле «Пароль/токен») или из подстановки {{password}}').format(name))
    return warnings


def _profile_warnings(preset: CommandPreset,
                      template: str) -> List[str]:
    """Проверить, что поля профиля, нужные команде, действительно заполнены."""
    warnings: List[str] = []
    for key, attribute, label in PROFILE_FIELDS:
        if key not in template:
            continue
        if not getattr(preset, attribute, None):
            warnings.append(t('в команде есть {}, но поле «{}» профиля пустое — подставится пустая строка').format(key, label))
    if "{port}" in template and preset.port and not 0 < preset.port <= 65535:
        warnings.append(t("порт в профиле должен быть от 1 до 65535"))
    return warnings


def _artifact_warnings(preset: CommandPreset, template: str,

                       artifacts: Dict[str, Any]) -> List[str]:
    """Предупредить, если команда ждёт файл, который сейчас не ведётся.

    Формат вывода выбирается в «Настройках»: «только список ссылок», «только
    конфиг для v2rayN» или «оба файла».  Если команда берёт файл, которого в
    текущем режиме нет, об этом лучше сказать заранее, а не в журнале запуска.
    """
    fmt = str(artifacts.get("format") or "")
    if fmt not in (OUTPUT_URI, OUTPUT_V2RAYN):
        return []
    warnings: List[str] = []
    wants_json = preset.artifact in ("json", "both") or "{json}" in template
    wants_links = preset.artifact in ("links", "both") or "{file}" in template
    if fmt == OUTPUT_URI and wants_json:
        warnings.append(t("конфиг для v2rayN не ведётся: в настройках выбран "
                        "формат «только список ссылок»"))
    if fmt == OUTPUT_V2RAYN and wants_links:
        warnings.append(t("список ссылок не ведётся: в настройках выбран формат "
                        "«только конфиг для v2rayN»"))
    return warnings


def render_preset(preset: CommandPreset, artifacts: Dict[str, Any],
                  when: Optional[float] = None) -> RenderedCommand:
    """Собрать командную строку пресета с подстановками.

    Путь к скрипту всегда заключается в кавычки (он может содержать пробелы), а
    значения подстановок подставляются как есть — если путь с пробелами, в
    шаблоне его нужно писать в кавычках, о чём предупредит список замечаний.
    """
    when = when or time.time()
    context = build_context(preset, artifacts, when)
    if preset.kind == "script":
        script = preset.script
        if script and " " in script and not script.startswith('"'):
            script = f'"{script}"'
        template = f"{script} {preset.arguments}".strip()
    else:
        template = preset.command

    warnings: List[str] = []
    if not template.strip():
        warnings.append(t("нечего запускать: не указан скрипт или команда"))
    command_line = template
    for key, value in context.items():
        command_line = command_line.replace(key, value)
    warnings += _placeholder_warnings(command_line, context)
    warnings += _env_warnings(command_line, preset)
    warnings += _profile_warnings(preset, template)
    warnings += _space_warnings(template, context)
    warnings += _artifact_warnings(preset, template, artifacts)
    return RenderedCommand(
        command_line=command_line,
        safe_command_line=mask_secret(command_line, preset.password),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Итог одного запуска внешней команды."""

    ok: bool = False
    code: Optional[int] = None
    output: str = ""
    error: str = ""
    duration: float = 0.0
    command_line: str = ""       # уже замаскированная (для журнала)

    @property
    def summary(self) -> str:
        if self.ok:
            return t("успешно") if self.code in (0, None) else t('код {}').format(self.code)
        return self.error or t('код {}').format(self.code)


def _decode_console(raw: bytes) -> str:
    """Расшифровать вывод консоли Windows (cp866/cp1251/utf-8)."""
    for encoding in ("utf-8", "cp866", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _kill_tree(process: "subprocess.Popen") -> None:
    """Убить процесс вместе с потомками (scp/curl могут плодить детей)."""
    if process.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=15)
        else:
            process.kill()
    except Exception:  # noqa: BLE001 - остановка не должна падать
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


def run_rendered(rendered: RenderedCommand, preset: CommandPreset,
                 timeout: Optional[float] = None) -> RunResult:
    """Выполнить уже собранную команду и вернуть результат."""
    started = time.time()
    result = RunResult(command_line=rendered.safe_command_line)
    if not rendered.command_line.strip():
        result.error = t("нечего запускать")
        return result

    cwd = preset.working_dir or None
    if cwd and not os.path.isdir(cwd):
        result.error = t('рабочий каталог не найден: {}').format(cwd)
        return result

    environment = os.environ.copy()
    for name, value in preset.env.items():
        environment[str(name)] = str(value)
    if preset.password:
        # Пароль доступен команде как переменная окружения: это безопаснее, чем
        # держать его в командной строке (её видно в списке процессов).
        environment.setdefault("COMMAND_PASSWORD", preset.password)

    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    limit = float(timeout or preset.timeout_seconds or 600.0)
    try:
        process = subprocess.Popen(
            rendered.command_line, shell=True, cwd=cwd, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=creationflags)
    except OSError as exc:
        result.error = t('не удалось запустить: {}').format(exc)
        result.duration = time.time() - started
        return result

    try:
        raw, _ = process.communicate(timeout=limit)
        result.code = process.returncode
        result.ok = process.returncode == 0
        if not result.ok:
            result.error = t('команда завершилась с кодом {}').format(process.returncode)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            raw, _ = process.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            raw = b""
        result.error = t('тайм-аут {} с — команда остановлена').format(int(limit))
        result.code = None
    except Exception as exc:  # noqa: BLE001
        _kill_tree(process)
        raw = b""
        result.error = str(exc)

    text = _decode_console(raw or b"").strip()
    result.output = text[-4000:]
    result.duration = time.time() - started
    return result


def run_preset(preset: CommandPreset, artifacts: Dict[str, Any],
               timeout: Optional[float] = None) -> RunResult:
    """Собрать и выполнить команду пресета.

    Подстановки раскрываются и в командной строке, и в переменных окружения.
    """
    rendered = render_preset(preset, artifacts)
    if preset.env:
        context = build_context(preset, artifacts, time.time())
        env: Dict[str, str] = {}
        for name, value in preset.env.items():
            text = str(value)
            for key, replacement in context.items():
                text = text.replace(key, replacement)
            env[str(name)] = text
        preset = replace(preset, env=env)
    return run_rendered(rendered, preset, timeout=timeout)


# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------

class CommandScheduler:
    """Триггеры, коалесцинг и запуск внешних команд.

    Работает независимо от цикла проверок:

    * ``on_change`` — запуск после того, как выходной файл реально изменился;
    * ``interval``  — запуск по расписанию;
    * ``manual``    — только по кнопке.

    Одновременно выполняется не более одной команды: остальные ждут в очереди
    (это защищает от лавины запусков, когда список рабочих серверов «дышит»).
    """

    def __init__(self, settings,
                 artifacts_provider: Optional[Callable[[], Dict]] = None,
                 runner: Callable[..., RunResult] = run_preset,
                 on_log: Optional[Callable[[str], None]] = None,
                 tick_seconds: float = 2.0) -> None:
        self.settings = settings
        self._artifacts = artifacts_provider or (lambda: {})
        self._runner = runner
        self._on_log = on_log
        self._tick = max(0.5, float(tick_seconds))
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._presets: List[CommandPreset] = []
        self._dirty: Dict[str, bool] = {}
        self._pending: List[str] = []
        self._next_run: Dict[str, float] = {}
        self._retry: Dict[str, Tuple[int, float]] = {}
        self._current: str = ""
        self.revision = 0
        self.load()

    # -- хранилище -------------------------------------------------------
    def load(self) -> None:
        raw = self.settings.get("simple_commands")
        items = raw if isinstance(raw, list) else []
        presets: List[CommandPreset] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            preset = CommandPreset.from_dict(item)
            if preset.script or preset.command:      # запускать нечего — пропуск
                presets.append(preset)
        self._presets = presets
        self._revision()
        self._reschedule()

    def _save(self) -> None:
        self.settings.set("simple_commands",
                          [preset.to_dict() for preset in self._presets])

    def _revision(self) -> None:
        with self._lock:
            self.revision += 1

    # -- доступ ----------------------------------------------------------
    def presets(self) -> List[CommandPreset]:
        with self._lock:
            return [CommandPreset.from_dict(preset.to_dict())
                    for preset in self._presets]

    def get(self, preset_id: str) -> Optional[CommandPreset]:
        with self._lock:
            for preset in self._presets:
                if preset.id == preset_id:
                    return preset
        return None

    def add(self, preset: CommandPreset) -> CommandPreset:
        stored = CommandPreset.from_dict(preset.to_dict())
        with self._lock:
            self._presets.append(stored)
        self._save()
        self._revision()
        self._reschedule()
        return stored

    def update(self, preset: CommandPreset) -> bool:
        changed = False
        with self._lock:
            for index, existing in enumerate(self._presets):
                if existing.id == preset.id:
                    self._presets[index] = CommandPreset.from_dict(
                        preset.to_dict())
                    changed = True
                    break
        if changed:
            self._save()
            self._revision()
            self._reschedule()
        return changed

    def remove(self, preset_id: str) -> bool:
        with self._lock:
            before = len(self._presets)
            self._presets = [preset for preset in self._presets
                             if preset.id != preset_id]
            removed = len(self._presets) != before
            self._pending = [item for item in self._pending
                             if item != preset_id]
        if removed:
            self._save()
            self._revision()
        return removed

    def duplicate(self, preset_id: str) -> Optional[CommandPreset]:
        source = self.get(preset_id)
        if source is None:
            return None
        return self.add(source.clone())

    def notify_event(self, event: str) -> None:
        """Событие автоматизации: помечаем пресеты с таким же триггером.

        События шлёт приложение: ``on_start``/``on_stop`` — запуск и остановка
        цикла, ``on_round`` — завершён круг проверок, ``on_network_down`` и
        ``on_network_up`` — пропала и вернулась сеть, ``on_change`` — выходной
        файл перезаписан.
        """
        if event not in EVENT_TRIGGERS:
            return
        with self._lock:
            for preset in self._presets:
                if preset.enabled and preset.trigger == event:
                    self._dirty[preset.id] = True

    def notify_output_changed(self) -> None:
        """Выходной файл изменился: помечаем пресеты ``on_change``."""
        self.notify_event("on_change")

    # -- управление ------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def busy(self) -> bool:
        return bool(self._current)

    @property
    def current_name(self) -> str:
        with self._lock:
            preset_id = self._current
            for preset in self._presets:
                if preset.id == preset_id:
                    return preset.name
        return ""

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._reschedule()
            thread = threading.Thread(target=self._loop,
                                      name="simple-commands", daemon=True)
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if (thread is not None and thread.is_alive()
                and threading.current_thread() is not thread):
            thread.join(timeout=timeout)

    def run_now(self, preset_id: str) -> bool:
        """Поставить пресет в начало очереди (ручной запуск)."""
        with self._lock:
            if self._current == preset_id:
                self._log(t("команда уже выполняется — повторный запуск пропущен"))
                return False
            if preset_id in self._pending:
                self._pending.remove(preset_id)
            self._pending.insert(0, preset_id)
        self._revision()
        return True

    def wait_idle(self, timeout: float = 120.0) -> bool:
        """Дождаться, пока очередь опустеет (режим без интерфейса).

        Учитываются только «выполнимые» задачи: если запуск ждёт минимальной
        паузы между проверками, ждать его бессмысленно.
        """
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if not self._has_runnable(time.time()):
                return True
            time.sleep(0.2)
        return False

    def _has_runnable(self, now: float) -> bool:
        """Есть ли команда, которую планировщик может запустить прямо сейчас."""
        with self._lock:
            if self._pending or self._current:
                return True
            for preset in self._presets:
                if not preset.enabled or not self._dirty.get(preset.id):
                    continue
                gap = float(preset.min_gap_seconds or 0.0)
                if not preset.last_run or now - preset.last_run >= gap:
                    return True
        return False

    # -- расписание ------------------------------------------------------
    def _reschedule(self) -> None:
        """Пересчитать времена запуска для триггера «по расписанию»."""
        now = time.time()
        with self._lock:
            for preset in self._presets:
                if preset.trigger != "interval" or not preset.enabled:
                    self._next_run.pop(preset.id, None)
                    continue
                interval = max(60.0, preset.interval_minutes * 60.0)
                if preset.last_run:
                    # Просроченные запускаем почти сразу после старта, но не
                    # устраиваем «догоняющий» шторм из нескольких запусков.
                    self._next_run[preset.id] = max(preset.last_run + interval,
                                                    now + 5.0)
                else:
                    self._next_run[preset.id] = now + interval

    def _log(self, message: str) -> None:
        log.info(t("Внешние команды: %s"), message)
        callback = self._on_log
        if callable(callback):
            try:
                callback(message)
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_once()
            except Exception as exc:  # noqa: BLE001 - планировщик не должен падать
                self._log(t('ошибка планировщика: {}').format(exc))
            self._stop.wait(timeout=self._tick)

    def _tick_once(self) -> None:
        """Обработать триггеры и выполнить одну команду из очереди."""
        now = time.time()
        with self._lock:
            for preset in self._presets:
                if not preset.enabled:
                    continue
                if preset.id in self._pending or preset.id == self._current:
                    continue
                due = False
                left, when = self._retry.get(preset.id, (0, 0.0))
                if left and now >= when:
                    # Повтор после ошибки: триггер уже сработал, ждём только паузу.
                    self._pending.append(preset.id)
                    continue
                if preset.trigger == "manual":
                    continue
                if preset.trigger == "interval":
                    due = now >= self._next_run.get(preset.id, now + 1.0)
                else:
                    # on_change и события автоматизации ждут отметки от
                    # приложения (см. notify_event).
                    due = bool(self._dirty.pop(preset.id, False))
                if not due:
                    continue
                gap = float(preset.min_gap_seconds or 0.0)
                if preset.last_run and now - preset.last_run < gap:
                    # Слишком частые изменения — вернём признак, попробуем позже.
                    if preset.trigger in EVENT_TRIGGERS:
                        self._dirty[preset.id] = True
                    continue
                if left and now < when:
                    continue
                self._pending.append(preset.id)

            preset_id = ""
            preset = None
            if not self._current and self._pending:
                preset_id = self._pending.pop(0)
                self._current = preset_id
                for item in self._presets:
                    if item.id == preset_id:
                        preset = item
                        break
                if preset is None:
                    self._current = ""
        if preset is None:
            return
        self._revision()
        try:
            self._execute(preset)
        finally:
            with self._lock:
                self._current = ""
            self._revision()

    # -- выполнение ------------------------------------------------------
    def _execute(self, preset: CommandPreset) -> None:
        """Выполнить пресет и записать результат в состояние и журнал."""
        artifacts = {}
        try:
            artifacts = self._artifacts() or {}
        except Exception as exc:  # noqa: BLE001
            self._log(t('не удалось получить пути файлов: {}').format(exc))
        rendered = render_preset(preset, artifacts)
        if preset.hidden:
            # Приватный пресет: в журнал не пишем ни команду, ни её вывод —
            # всё это видно только в интерфейсе и только по явному запросу.
            self._log(t('запуск «{}»: команда скрыта (пресет помечен как скрытый)').format(preset.name))
        else:
            self._log(t('запуск «{}»: {}').format(preset.name, rendered.safe_command_line))
        for warning in rendered.warnings:
            self._log(t('предупреждение «{}»: {}').format(preset.name, warning))
        try:
            result = self._runner(preset, artifacts)
        except Exception as exc:  # noqa: BLE001 - запуск не должен падать
            result = RunResult(error=t('внутренняя ошибка: {}').format(exc),
                               command_line=rendered.safe_command_line)
        self._apply_result(preset, result)

    def _apply_result(self, preset: CommandPreset, result: RunResult) -> None:
        preset.last_run = time.time()
        preset.last_code = result.code
        preset.last_duration = result.duration
        preset.last_output = result.output
        tail = result.output.strip()

        if result.ok:
            preset.last_status = "успешно"
            message = t('«{}»: успешно за {:.1f} с').format(preset.name, result.duration)
            message += self._tail_note(preset, tail, 300)
            self._retry.pop(preset.id, None)
        else:
            preset.last_status = f"ошибка: {result.error or 'код ' + str(result.code)}"
            message = f"«{preset.name}»: {result.error or t('ошибка')}"
            message += self._tail_note(preset, tail, 600)
            left, _when = self._retry.get(preset.id, (0, 0.0))
            if not left and preset.retry_count:
                left = preset.retry_count
            if left > 0 and result.code is not None and result.duration > 0:
                self._retry[preset.id] = (left - 1,
                                          time.time() + preset.retry_delay_seconds)
                message += (t(' | повтор через {} с').format(int(preset.retry_delay_seconds)))

        with self._lock:
            for index, item in enumerate(self._presets):
                if item.id == preset.id:
                    self._presets[index] = preset
                    break
        self._save()
        self._reschedule()
        self._revision()
        self._log(message)

    @staticmethod
    def _tail_note(preset: CommandPreset, tail: str, limit: int) -> str:
        """Хвост вывода для журнала.

        В выводе команды могут оказаться ссылки, идентификаторы и токены, поэтому
        для пресета с флагом «скрытый» в журнал уходит только пометка: сам текст
        виден лишь в интерфейсе и только по явному запросу.
        """
        if not tail:
            return ""
        if preset.hidden:
            return t(" | вывод скрыт (пресет помечен как скрытый)")
        return t(' | вывод: {}').format(tail[-limit:])
