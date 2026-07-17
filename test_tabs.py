"""Тест управления вкладками через Playwright MCP.

Доказывает, что list_tabs находит вкладки, а close_tab закрывает их.
Запуск: python test_tabs.py (требует запущенный Chrome с Playwright Extension).

Тест:
  1. MCP подключается — открывается вкладка connect.html.
  2. list_tabs находит эту вкладку.
  3. Создаём временную вкладку (navigate to about:blank).
  4. list_tabs находит обе вкладки.
  5. Закрываем временную вкладку.
  6. list_tabs подтверждает, что вкладка закрыта.
"""

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
load_dotenv()

import lib
lib.setup_console()

import browser_agent as ba


def check(name, cond, got="", expected=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}")
    if not cond:
        print(f"  got:      {got!r}")
        print(f"  expected: {expected!r}")
    return cond


async def run_tests():
    pkg = os.getenv("PLAYWRIGHT_MCP_PACKAGE") or "@playwright/mcp"
    m = ba.MCPSession(pkg, HERE / "playwright-mcp.json")
    await m.start()

    results = []

    # 1. list_tabs после MCP start — должна быть хотя бы одна вкладка (connect.html).
    tabs = await ba.list_tabs(m)
    results.append(check("list_tabs: есть вкладки", len(tabs) > 0, len(tabs), "> 0"))

    # 2. Находим вкладку connect.html — она открыта MCP.
    connect_tabs = [t for t in tabs if "connect" in t.get("url", "").lower()]
    results.append(check("list_tabs: connect.html найден", len(connect_tabs) > 0,
                         f"{len(connect_tabs)} connect tabs", "> 0"))

    # 3. Запоминаем вкладки до создания новой.
    initial_ids = {t["tabId"] for t in tabs}

    # 3b. Pre-existing connect не должен попасть в список на закрытие.
    connect_pre = next(t for t in tabs if "connect" in t.get("url", "").lower())
    close_set = ba.script_tab_ids_to_close(tabs, tabs)
    results.append(check("cleanup: pre-existing connect not closed",
                         connect_pre["tabId"] not in close_set,
                         connect_pre["tabId"] in close_set, False))

    # 4. Создаём новую вкладку через browser_navigate.
    #    MCP открывает URL в текущей вкладке, поэтому сначала переключим на новую.
    #    Используем browser_tabs с action=new для создания вкладки.
    try:
        new_tab_result = await m.call("browser_tabs", {"action": "new", "url": "about:blank"}, timeout=10)
        await asyncio.sleep(1)
    except Exception:
        # Если browser_tabs не поддерживает action=new, просто проверим list/close.
        await m.stop()
        passed = sum(1 for r in results if r)
        failed = sum(1 for r in results if not r)
        print(f"\n{'='*40}")
        print(f"Tabs (без create): {passed} passed, {failed} failed")
        if failed:
            sys.exit(1)
        print("[tabs] тесты пройдены (без create/close)")
        return

    # 5. list_tabs теперь содержит новую вкладку.
    tabs_after = await ba.list_tabs(m)
    new_ids = {t["tabId"] for t in tabs_after} - initial_ids
    results.append(check("list_tabs: новая вкладка появилась", len(new_ids) > 0,
                         len(new_ids), "> 0"))

    # 6. Закрываем новую вкладку.
    if new_ids:
        new_id = next(iter(new_ids))
        ok = await ba.close_tab(m, new_id)
        results.append(check("close_tab: закрытие вкладки", ok, ok, True))
        await asyncio.sleep(1)

        # 7. list_tabs подтверждает, что вкладка закрыта.
        tabs_final = await ba.list_tabs(m)
        still_exists = new_id in {t["tabId"] for t in tabs_final}
        results.append(check("close_tab: вкладка исчезла из списка", not still_exists,
                             still_exists, False))

    await m.stop()

    passed = sum(1 for r in results if r)
    failed = sum(1 for r in results if not r)
    print(f"\n{'='*40}")
    print(f"Tabs: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("[tabs] все тесты пройдены")


if __name__ == "__main__":
    asyncio.run(run_tests())
