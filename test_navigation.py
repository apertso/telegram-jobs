"""Тесты навигации Telegram Web SPA.

Доказывает, какие способы смены канала работают, а какие — нет.
Запуск: python test_navigation.py (требует запущенный Chrome с Playwright Extension).
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

URL_1 = "https://web.telegram.org/k/#@evacuatejobs"
URL_2 = "https://web.telegram.org/k/#@g_jobbot"


async def _eval(mcp, js):
    r = await mcp.call("browser_evaluate", {"function": js}, timeout=10)
    if "### Result" in r:
        s = r.index("### Result") + len("### Result")
        e = r.find("### Ran", s)
        if e == -1:
            e = len(r)
        return r[s:e].strip().strip('"').strip().strip("\\n").strip()
    return r


async def _reset(mcp):
    """Сброс на канал 1 через about:blank."""
    await mcp.call("browser_navigate", {"url": "about:blank"}, timeout=10)
    await mcp.call("browser_navigate", {"url": URL_1}, timeout=15)
    await asyncio.sleep(5)


async def _hash_and_msg(mcp):
    h = await _eval(mcp, "() => location.hash")
    m = await _eval(mcp, "() => { const el = document.querySelector('[data-mid] .text') || document.querySelector('[data-mid]'); return el ? el.innerText.slice(0,80) : 'none'; }")
    return h, m


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

    # Базовое состояние: канал 1.
    await _reset(m)
    h1, msg1 = await _hash_and_msg(m)
    results.append(check("база: hash = #@evacuatejobs", h1 == "#@evacuatejobs", h1, "#@evacuatejobs"))

    # --- 1. browser_navigate на тот же домен с другим hash (2-й раз) НЕ работает ---
    await m.call("browser_navigate", {"url": URL_2}, timeout=15)
    await asyncio.sleep(3)
    h, msg = await _hash_and_msg(m)
    results.append(check("browser_navigate 2-й раз: НЕ работает (FAIL expected)",
                         h == "#@evacuatejobs" and msg == msg1))

    # --- 2. location.hash НЕ открывает канал ---
    await _reset(m)
    await _eval(m, "() => { location.hash = '#@g_jobbot'; return location.hash; }")
    await asyncio.sleep(3)
    h, msg = await _hash_and_msg(m)
    results.append(check("location.hash: канал НЕ меняется (FAIL expected)",
                         msg == msg1, msg[:80], "!= " + msg1[:80]))

    # --- 3. location.replace НЕ открывает канал ---
    await _reset(m)
    await _eval(m, "() => { location.replace('https://web.telegram.org/k/#@g_jobbot'); return 'ok'; }")
    await asyncio.sleep(3)
    h, msg = await _hash_and_msg(m)
    results.append(check("location.replace: канал НЕ меняется (FAIL expected)",
                         msg == msg1, msg[:80], "!= " + msg1[:80]))

    # --- 4. history.pushState + popstate НЕ работает ---
    await _reset(m)
    await _eval(m, "() => { history.pushState(null, '', '#@g_jobbot'); window.dispatchEvent(new PopStateEvent('popstate')); return location.hash; }")
    await asyncio.sleep(3)
    h, msg = await _hash_and_msg(m)
    results.append(check("pushState + popstate: канал НЕ меняется (FAIL expected)",
                         msg == msg1, msg[:80], "!= " + msg1[:80]))

    # --- 5. hashchange dispatch НЕ работает ---
    await _reset(m)
    await _eval(m, "() => { location.hash = '#@g_jobbot'; window.dispatchEvent(new HashChangeEvent('hashchange')); return location.hash; }")
    await asyncio.sleep(3)
    h, msg = await _hash_and_msg(m)
    results.append(check("hashchange dispatch: канал НЕ меняется (FAIL expected)",
                         msg == msg1, msg[:80], "!= " + msg1[:80]))

    # --- 6. about:blank → полный URL РАБОТАЕТ ---
    await _reset(m)
    await m.call("browser_navigate", {"url": "about:blank"}, timeout=10)
    await m.call("browser_navigate", {"url": URL_2}, timeout=15)
    await asyncio.sleep(5)
    h, msg = await _hash_and_msg(m)
    results.append(check("about:blank → URL: hash = #@g_jobbot",
                         h == "#@g_jobbot", h, "#@g_jobbot"))
    results.append(check("about:blank → URL: контент меняется",
                         msg != msg1, msg[:80], "!= " + msg1[:80]))

    await m.stop()

    passed = sum(1 for r in results if r)
    failed = sum(1 for r in results if not r)
    print(f"\n{'='*40}")
    print(f"Навигация: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("[navigation] все тесты пройдены")


if __name__ == "__main__":
    asyncio.run(run_tests())
