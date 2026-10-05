"""Точка входа simple-версии (см. ``simple/cli.py``).

    python simple_main.py                 # графический интерфейс
    python simple_main.py --headless      # цикл без интерфейса
    python simple_main.py --once          # один проход

Без окна консоли программа запускается через ``pythonw.exe`` — например,
двойным кликом по ``simple_main.pyw`` (Windows открывает ``.pyw`` оконным
интерпретатором) или ярлыком, который создаёт автозагрузка.
"""

from __future__ import annotations

import sys

from simple.cli import main

if __name__ == "__main__":
    sys.exit(main())
