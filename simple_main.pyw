"""Запуск без окна консоли: ``simple_main.pyw`` открывается двойным кликом.

Windows связывает расширение ``.pyw`` с ``pythonw.exe`` — оконным
интерпретатором, который консольного окна не создаёт.  Это штатный способ
запустить программу «как приложение», не собирая exe: то же самое делают ярлык
с ``pythonw.exe`` и автозагрузка (она ставит ``--tray``).

Для работы в терминале (``--headless``, ``--once``, ``--import``, ``--stats``)
используйте ``simple_main.py`` — там консоль нужна по смыслу, потому что в неё
печатается результат.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
