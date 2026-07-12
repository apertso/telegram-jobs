"""CLI для Telegram Jobs Collector.

Команды
-------
  python run.py            Полный сбор через Playwright MCP (collect.py):
                           подключение к текущему Chrome через Playwright
                           Extension, агентный сбор и извлечение вакансий.
                           Требует запущенный Chrome с Playwright Extension и
                           активную сессию Telegram Web.

  python run.py --selftest Запуск встроенных проверок ядра (lib + server).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lib

lib.setup_console()


def run_full() -> None:
    """Полный сбор через Playwright MCP (collect.py)."""
    import collect

    asyncio.run(collect.main())


def run_selftest() -> None:
    import test_lib  # noqa: F401  (скрипт сам делает assert/exit при ошибке)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Jobs Collector CLI")
    parser.add_argument(
        "--selftest", action="store_true", help="Встроенные проверки ядра"
    )
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    else:
        run_full()


if __name__ == "__main__":
    main()
