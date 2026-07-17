"""Тесты навигации Telegram Web SPA.

Доказывает, какие способы смены канала работают, а какие — нет.
Запуск: python test_navigation.py (требует запущенный Chrome с Playwright Extension).
"""

import asyncio
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


async def _hash_and_message_fingerprint(mcp):
    h = await _eval(mcp, "() => location.hash")
    fingerprint = await _eval(mcp, "() => { const el = document.querySelector('[data-mid] .text') || document.querySelector('[data-mid]'); const value = el ? (el.innerText || '') : ''; let hash = 2166136261; for (let i = 0; i < value.length; i++) { hash ^= value.charCodeAt(i); hash = Math.imul(hash, 16777619); } return (hash >>> 0).toString(16); }")
    return h, fingerprint


def check(name, cond, got="", expected=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}")
    if not cond:
        print(f"  got:      {got!r}")
        print(f"  expected: {expected!r}")
    return cond


async def run_tests():
    m = ba.MCPSession(HERE / "playwright-mcp.json")
    await m.start()

    results = []

    # Базовое состояние: канал 1.
    await _reset(m)
    h1, fingerprint1 = await _hash_and_message_fingerprint(m)
    results.append(check("база: hash = #@evacuatejobs", h1 == "#@evacuatejobs", h1, "#@evacuatejobs"))

    # --- 1. browser_navigate на тот же домен с другим hash (2-й раз) НЕ работает ---
    await m.call("browser_navigate", {"url": URL_2}, timeout=15)
    await asyncio.sleep(3)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("browser_navigate 2-й раз: НЕ работает (FAIL expected)",
                         h == "#@evacuatejobs" and fingerprint == fingerprint1))

    # --- 2. location.hash НЕ открывает канал ---
    await _reset(m)
    await _eval(m, "() => { location.hash = '#@g_jobbot'; return location.hash; }")
    await asyncio.sleep(3)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("location.hash: канал НЕ меняется (FAIL expected)",
                         fingerprint == fingerprint1, fingerprint, "different fingerprint"))

    # --- 3. location.replace НЕ открывает канал ---
    await _reset(m)
    await _eval(m, "() => { location.replace('https://web.telegram.org/k/#@g_jobbot'); return 'ok'; }")
    await asyncio.sleep(3)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("location.replace: канал НЕ меняется (FAIL expected)",
                         fingerprint == fingerprint1, fingerprint, "different fingerprint"))

    # --- 4. history.pushState + popstate НЕ работает ---
    await _reset(m)
    await _eval(m, "() => { history.pushState(null, '', '#@g_jobbot'); window.dispatchEvent(new PopStateEvent('popstate')); return location.hash; }")
    await asyncio.sleep(3)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("pushState + popstate: канал НЕ меняется (FAIL expected)",
                         fingerprint == fingerprint1, fingerprint, "different fingerprint"))

    # --- 5. hashchange dispatch НЕ работает ---
    await _reset(m)
    await _eval(m, "() => { location.hash = '#@g_jobbot'; window.dispatchEvent(new HashChangeEvent('hashchange')); return location.hash; }")
    await asyncio.sleep(3)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("hashchange dispatch: канал НЕ меняется (FAIL expected)",
                         fingerprint == fingerprint1, fingerprint, "different fingerprint"))

    # --- 6. about:blank → полный URL РАБОТАЕТ ---
    await _reset(m)
    await m.call("browser_navigate", {"url": "about:blank"}, timeout=10)
    await m.call("browser_navigate", {"url": URL_2}, timeout=15)
    await asyncio.sleep(5)
    h, fingerprint = await _hash_and_message_fingerprint(m)
    results.append(check("about:blank → URL: hash = #@g_jobbot",
                         h == "#@g_jobbot", h, "#@g_jobbot"))
    results.append(check("about:blank → URL: контент меняется",
                         fingerprint != fingerprint1, fingerprint, "different fingerprint"))

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
