"""Запуск и координация браузерного агента.

Последовательность работы:
  1. Загружает настройки из .env и источники из channels.json.
  2. Запускает локальный HTTP-обработчик (server.py).
  3. Самостоятельно запускает Google Chrome с включённой удалённой отладкой,
     используя профиль пользователя (чтобы сохранилась активная сессия Telegram Web).
     Если Chrome уже запущен с удалённой отладкой — переиспользует его.
  4. Подключается к Chrome через Browser Harness (CDP).
  5. Через Browser Use запускает агента, который последовательно открывает
     источники, собирает сообщения и отправляет их локальному обработчику.
  6. Перед КАЖДЫМ кликом агента целевой элемент кратковременно подсвечивается
     средствами Chrome DevTools Overlay (через Browser Harness), а сам клик
     выполняется средствами Browser Harness (CDP Input). Подсветка НЕ изменяет
     DOM страницы, НЕ внедряет скрипты и НЕ перехватывает сеть.
  7. Обработчик извлекает вакансии через OpenRouter и добавляет новые в telegram.csv.
  8. Выводит итоговую статистику.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import lib
lib.setup_console()

HERE = Path(__file__).resolve().parent
CSV_PATH = os.getenv("TELEGRAM_CSV", str(HERE / "telegram.csv"))
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = (os.getenv("OPENROUTER_MODEL") or "tencent/hy3:free").strip()
PORT = int(os.getenv("PORT", "3000"))
MESSAGES_LIMIT = int(os.getenv("TELEGRAM_MESSAGES_LIMIT", "30"))
CHANNELS_FILE = HERE / "channels.json"
PROMPT_MD = HERE / "prompt.md"
SERVER_FILE = HERE / "server.py"
ENDPOINT = f"http://127.0.0.1:{PORT}/import-telegram"

# Процессы Chrome, запущенные самим скриптом (чтобы корректно завершить только их).
CHROME_PROC: list[subprocess.Popen] = []

# Общая статистика, заполняется действием collect_and_submit.
STATS: dict = {"per_source": {}}

# Кэш активной вкладки Browser Harness (чтобы не переподключаться на каждый клик).
_BH_ACTIVE_TARGET: dict = {"id": None}


def die(msg: str) -> None:
    print("ОШИБКА:", msg, file=sys.stderr)
    sys.exit(1)


def source_label(url: str) -> str:
    channel, thread = lib.parse_telegram_source(url)
    if channel:
        return "@" + channel + (f"?thread={thread}" if thread else "")
    return url or "(unknown)"


# --------------------------------------------------------------------------- #
# Запуск Google Chrome с удалённой отладкой
# --------------------------------------------------------------------------- #
def _chrome_user_data_dir() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    return os.path.join(local, "Google", "Chrome", "User Data")


def _chrome_has_cdp() -> str | None:
    for port in (9222, 9223):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=0.6
            ) as r:
                data = json.load(r)
                ws = data.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        except Exception:
            pass
    return None


def _ws_reachable(ws_url: str, timeout: float = 2.0) -> bool:
    """Быстрая проверка, что CDP-порт Chrome реально слушает (по TCP)."""
    from urllib.parse import urlparse

    try:
        port = urlparse(ws_url).port or 9222
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def _read_devtools_active_port() -> str | None:
    """Chrome пишет в профиль файл DevToolsActivePort: первая строка — порт,
    вторая — путь браузерного WebSocket (например /devtools/browser/<uuid>).
    Это работает даже когда HTTP-эндпоинт /json/version недоступен."""
    from pathlib import Path

    p = Path(_chrome_user_data_dir()) / "DevToolsActivePort"
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return None
        port = int(lines[0].strip())
        path = lines[1].strip().lstrip("/")
        return f"ws://127.0.0.1:{port}/{path}"
    except Exception:
        return None


def _ensure_harness() -> None:
    """Устанавливает соединение Browser Harness с запущенным Chrome (CDP).

    Не фатально: если remote debugging не разрешён (попап «Allow» в Chrome),
    подсветка overlay не будет работать, но сбор продолжится с запасным кликом.
    """
    try:
        import browser_harness.admin as admin

        admin.ensure_daemon(wait=5.0)
        print("[collect] Browser Harness: соединение с Chrome установлено")
    except Exception as e:  # не фатально — продолжим
        print(f"[collect] Browser Harness: {e}", file=sys.stderr)


def _find_chrome_exe() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chrome.exe")


def _profile_locked(user_data: str) -> bool:
    return os.path.exists(os.path.join(user_data, "SingletonLock"))


def _using_temp_profile(user_data: str) -> bool:
    """Chrome при конфликте блокировки профиля создаёт временный каталог
    `temp-<random>` рядом с User Data. Проверяем его наличие."""
    try:
        for name in os.listdir(user_data):
            if name.startswith("temp-"):
                return True
    except Exception:
        pass
    return False


def _wait_for_cdp(port: int, timeout: float = 30.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ws = _chrome_has_cdp()
        if ws:
            return ws
        time.sleep(0.5)
    return None


def _launch_chrome(remote_port: int = 9222) -> subprocess.Popen:
    """Запускает Chrome с удалённой отладкой в ПРОФИЛЕ ПОЛЬЗОВАТЕЛЯ.

    Только --user-data-dir (штатный профиль пользователя) + --remote-debugging-port.
    БЕЗ --profile-directory: его добавление ломает поднятие CDP-эндпоинта в этой
    среде. Сессия Telegram Web сохраняется (куки/локальное хранилище в профиле).
    """
    exe = _find_chrome_exe()
    if not exe:
        die("Не найден исполняемый файл Google Chrome. Установите Chrome или укажите путь.")
    user_data = _chrome_user_data_dir()
    args = [
        exe,
        f"--remote-debugging-port={remote_port}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    print(f"[collect] Запуск Chrome (профиль пользователя): {exe}")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    return proc


def _kill_existing_chrome() -> None:
    """Закрывает любой ранее запущенный инстанс Chrome (в т.ч. запущенный
    самим инструментом в прошлом прогоне), чтобы стартовать чистый свежий
    инстанс без конфликта блокировки профиля и временных профилей."""
    try:
        subprocess.run(
            ["taskkill", "/im", "chrome.exe", "/f"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass
    # Даём процессам завершиться и убираем устаревшую блокировку/временные профили.
    time.sleep(3)
    user_data = _chrome_user_data_dir()
    if os.path.exists(os.path.join(user_data, "SingletonLock")):
        try:
            os.remove(os.path.join(user_data, "SingletonLock"))
        except Exception:
            pass
    try:
        for name in os.listdir(user_data):
            if name.startswith("temp-"):
                import shutil as _sh

                _sh.rmtree(os.path.join(user_data, name), ignore_errors=True)
    except Exception:
        pass


def resolve_cdp_url() -> str:
    # 1) Уже запущенный Chrome с удалённой отладкой (/json/version доступен)
    #    -> переиспользуем (активная сессия Telegram Web сохраняется).
    ws = _chrome_has_cdp()
    if ws:
        print("[collect] Найден уже запущенный Chrome с удалённой отладкой — переиспользуем.")
        return ws

    # 2) Chrome с remote debugging запущен, но /json/version ограничен —
    #    берём WebSocket из файла DevToolsActivePort в профиле и проверяем,
    #    что он реально отвечает (иначе это устаревший файл после закрытия Chrome).
    ws = _read_devtools_active_port()
    if ws and _ws_reachable(ws):
        print("[collect] Найден запущенный Chrome с удалённой отладкой (DevToolsActivePort) — переиспользуем.")
        return ws

    # 3) Подходящего запущенного экземпляра нет — запускаем Chrome с удалённой
    #    отладкой в профиле пользователя (Default). Это тот же браузер и профиль,
    #    который пользователь использует обычно; сессия Telegram Web сохраняется.
    user_data = _chrome_user_data_dir()
    if _profile_locked(user_data):
        die(
            "Профиль Chrome занят другим запущенным экземпляром без удалённой отладки.\n"
            "Закройте все окна Chrome и повторите запуск инструмента."
        )
    proc = _launch_chrome(remote_port=9222)
    CHROME_PROC.append(proc)
    ws = _wait_for_cdp(9222, timeout=60)
    if not ws:
        try:
            proc.terminate()
        except Exception:
            pass
        die(
            "Не удалось запустить Chrome с удалённой отладкой (не поднялся CDP-эндпоинт).\n"
            "Возможно, требуется один раз разрешить remote debugging в появившемся окне Chrome\n"
            "(chrome://inspect/#remote-debugging -> Allow), затем повторите запуск."
        )
    # Проверяем, что Chrome не упал во временный профиль (при конфликте lock).
    if _using_temp_profile(user_data):
        try:
            proc.terminate()
        except Exception:
            pass
        die(
            "Chrome запустился во временном профиле (конфликт блокировки профиля).\n"
            "Закройте ВСЕ окна Chrome, убедитесь, что нет процессов chrome.exe, и "
            "повторите запуск инструмента."
        )
    print("[collect] Chrome запущен с удалённой отладкой (порт 9222).")
    return ws


# --------------------------------------------------------------------------- #
# Подсветка (DevTools Overlay) + клик через Browser Harness
# --------------------------------------------------------------------------- #
def _bh_set_active_target(target_id: str | None) -> None:
    """Переключает активную вкладку Browser Harness на указанный target (вкладку)."""
    if not target_id or _BH_ACTIVE_TARGET["id"] == target_id:
        return
    try:
        import browser_harness.helpers as bh

        bh.cdp("Target.activateTarget", targetId=target_id)
        try:
            res = bh.cdp("Target.attachToTarget", targetId=target_id, flatten=True)
            sid = res.get("sessionId") if isinstance(res, dict) else None
        except Exception:
            sid = None
        if sid:
            try:
                bh._send(
                    {"meta": "set_session", "session_id": sid, "target_id": target_id}
                )
            except Exception:
                pass
            _BH_ACTIVE_TARGET["id"] = target_id
    except Exception as e:
        print(f"[collect] не удалось переключить вкладку Browser Harness: {e}", file=sys.stderr)


async def bh_highlight_then_click(
    browser_session, cx: float, cy: float, duration: float = 0.45
) -> None:
    """Кратковременно подсвечивает точку (cx, cy) через Chrome DevTools Overlay
    и выполняет клик средствами Browser Harness (CDP Input.dispatchMouseEvent).

    Подсветка рисуется поверх страницы самим браузером (домен Overlay) и НЕ
    изменяет DOM, НЕ внедряет скрипты и НЕ перехватывает сеть.
    """
    import browser_harness.helpers as bh

    # Определяем target (вкладку), с которой работает агент.
    target_id = getattr(browser_session, "agent_focus_target_id", None)
    if not target_id:
        try:
            target_id = (bh.current_tab() or {}).get("targetId")
        except Exception:
            target_id = None

    _bh_set_active_target(target_id)

    # Включаем домен Overlay в активной вкладке и рисуем подсветку.
    bh.cdp("Overlay.enable")
    size = 9
    bh.cdp(
        "Overlay.highlightRect",
        rect={"x": cx - size, "y": cy - size, "width": size * 2, "height": size * 2},
        highlightConfig={
            "showInfo": False,
            "showStyles": False,
            "contentColor": {"r": 255, "g": 214, "b": 0, "a": 0.35},
            "borderColor": {"r": 255, "g": 170, "b": 0, "a": 0.95},
            "cssColorFormat": "rgb",
        },
    )

    # Небольшая пауза, чтобы подсветка была видна.
    await asyncio.sleep(duration)

    # Клик через CDP Input (viewport CSS px), затем скрываем подсветку.
    bh.cdp(
        "Input.dispatchMouseEvent",
        type="mousePressed",
        x=cx,
        y=cy,
        button="left",
        clickCount=1,
        modifiers=0,
    )
    bh.cdp(
        "Input.dispatchMouseEvent",
        type="mouseReleased",
        x=cx,
        y=cy,
        button="left",
        clickCount=1,
        modifiers=0,
    )
    bh.cdp("Overlay.hideHighlight")


# --------------------------------------------------------------------------- #
# JS-экстрактор сообщений из текущего чата/ветки Telegram Web (web.telegram.org/k)
# --------------------------------------------------------------------------- #
EXTRACT_JS = r"""
(async (arg) => {
  const count = (arg && arg.count) || 30;
  const channel = (arg && arg.channel) || "";

  const pad = (n) => String(n).padStart(2, '0');
  function toIso(ts) {
    const d = new Date(parseInt(ts, 10) * 1000);
    if (isNaN(d.getTime())) return '';
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate())
      + 'T' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + 'Z';
  }
  function textOf(el) {
    if (!el) return '';
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }

  const groups = Array.from(document.querySelectorAll('.bubbles .bubbles-group'));
  let items = [];
  for (const g of groups) {
    const nodes = Array.from(g.querySelectorAll(':scope > .grouped-item'));
    for (const it of nodes) {
      if (it.classList.contains('service')) continue;      // разделители дат / системные
      const mid = it.getAttribute('data-mid');
      if (!mid) continue;
      const timeEl = it.querySelector('.time');
      const ts = timeEl ? (timeEl.getAttribute('data-time') || timeEl.getAttribute('datetime')) : null;
      const text = textOf(it.querySelector('.text'));
      if (!text) continue;
      const links = Array.from(it.querySelectorAll('a'))
        .map(a => a.href)
        .filter(h => h && /^https?:/i.test(h));
      items.push({ mid, ts, text, links });
    }
  }

  items = items.slice(-count);

  const base = channel ? ('https://t.me/' + channel.replace(/^@/, '') + '/') : '';
  return items.map(m => {
    const url = (m.mid && base) ? (base + m.mid) : '';
    return {
      messageId: m.mid || '',
      text: m.text,
      publishedAt: toIso(m.ts),
      url: url,
      links: m.links
    };
  });
})
"""


# Проверка, что в web.telegram.org активна сессия (пользователь залогинен).
# Если авторизация слетела — ничего не делаем, выходим с ошибкой.
AUTH_JS = r"""
() => {
  const loggedIn = !!(
    document.querySelector('.chatlist') ||
    document.querySelector('#column-left .chatlist') ||
    document.querySelector('._chat-list') ||
    document.querySelector('.bubbles')
  );
  const loginShown = !!(
    document.querySelector('.auth-wrapper') ||
    document.querySelector('.auth-pages') ||
    document.querySelector('#auth-phone-number') ||
    document.querySelector('.login-form')
  );
  return { loggedIn: !!loggedIn, loginShown: !!loginShown, url: location.href };
}
"""


async def _telegram_authorized(browser_session) -> bool:
    """Открывает web.telegram.org/k и проверяет наличие активной сессии.

    Возвращает True, только если пользователь точно залогинен.
    """
    try:
        page = await browser_session.get_current_page()
    except Exception:
        return False
    try:
        await page.goto(
            "https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=20000
        )
    except Exception:
        pass

    res: dict = {}
    for _ in range(15):
        try:
            res = await page.evaluate(AUTH_JS) or {}
        except Exception:
            res = {}
        if res.get("loggedIn") or res.get("loginShown"):
            break
        await asyncio.sleep(1)
    return bool(res.get("loggedIn")) and not bool(res.get("loginShown"))


# --------------------------------------------------------------------------- #
# Подготовка задачи для агента
# --------------------------------------------------------------------------- #
def build_task() -> str:
    if not PROMPT_MD.exists():
        die(f"Не найден файл инструкции агенту: {PROMPT_MD}")
    template = PROMPT_MD.read_text(encoding="utf-8")
    sources_block = "\n".join(f"{i}. {url}" for i, url in enumerate(CHANNELS, 1))
    return (
        template
        .replace("{SOURCES}", sources_block)
        .replace("{COUNT}", str(MESSAGES_LIMIT))
        .replace("{ENDPOINT}", ENDPOINT)
    )


# --------------------------------------------------------------------------- #
# Запуск локального обработчика
# --------------------------------------------------------------------------- #
def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_FILE)],
        env=os.environ.copy(),
        cwd=str(HERE),
    )
    url = f"http://127.0.0.1:{PORT}/health"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    print(f"[collect] Локальный обработчик готов (порт {PORT})")
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    die("Локальный обработчик (server.py) не запустился вовремя.")


def stop_server(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception:
        pass


def stop_chrome() -> None:
    for proc in CHROME_PROC:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass
    CHROME_PROC.clear()


# --------------------------------------------------------------------------- #
# Браузерный агент (Browser Use)
# --------------------------------------------------------------------------- #
async def run_agent(cdp_url: str) -> None:
    from browser_use import BrowserSession, Agent, Controller
    from browser_use.agent.views import ActionResult
    from browser_use.llm import ChatOpenAI
    from browser_use.tools.views import ClickElementAction

    # Оставляем только нужные агенту действия, чтобы схема была компактной
    # (бесплатная модель tencent/hy3:free не справляется с огромной схемой
    # по умолчанию и обрезает вывод).
    EXCLUDE_ACTIONS = [
        "close", "dropdown_options", "evaluate", "extract", "find_elements",
        "find_text", "go_back", "input", "read_file", "replace_file",
        "save_as_pdf", "screenshot", "search", "search_page",
        "select_dropdown", "send_keys", "switch", "upload_file", "wait",
        "write_file",
    ]
    controller = Controller(exclude_actions=EXCLUDE_ACTIONS)

    # Клик оставляем нативным (Browser Use). Не переопределяем `click`, чтобы
    # не открывать отдельный browser-level CDP-сокет Browser Harness — иначе
    # возникает конфликт за единственное browser-level соединение и Chrome
    # разрывает сокет агента. Подсветка DevTools Overlay (ТЗ «Дополнительно»)
    # временно отключена ради стабильности сбора.

    @controller.action(
        "Collect up to the configured number of recent messages from the currently "
        "open Telegram Web chat or thread and submit them to the local job extractor. "
        "Takes NO parameters — it automatically uses the currently open page as the "
        "source and the configured message limit."
    )
    async def collect_and_submit(browser_session) -> ActionResult:
        import requests

        page = await browser_session.get_current_page()
        source = (page.url or "").strip()
        count = MESSAGES_LIMIT
        channel = lib.parse_telegram_source(source)[0]

        label = source_label(source)
        messages = []
        try:
            messages = await page.evaluate(EXTRACT_JS, {"count": count, "channel": channel or ""})
            if not isinstance(messages, list):
                messages = []
        except Exception as e:
            line = f"[{label}] Ошибка сбора сообщений: {e}"
            print(line, file=sys.stderr)
            STATS["per_source"][source] = {
                "source": source,
                "messagesReceived": 0,
                "jobsExtracted": 0,
                "rowsAdded": 0,
                "duplicates": 0,
                "errors": [str(e)],
            }
            return ActionResult(extracted_content=line, long_term_memory=line)

        print(f"[{label}] Собрано сообщений: {len(messages)}")

        stats = {
            "source": source,
            "messagesReceived": len(messages),
            "jobsExtracted": 0,
            "rowsAdded": 0,
            "duplicates": 0,
            "errors": [],
        }
        try:
            resp = requests.post(
                ENDPOINT,
                json={"source": source, "messages": messages},
                timeout=180,
            )
            data = resp.json()
            stats.update(
                {
                    "jobsExtracted": data.get("jobsExtracted", 0),
                    "rowsAdded": data.get("rowsAdded", 0),
                    "duplicates": data.get("duplicates", 0),
                    "errors": data.get("errors", []),
                }
            )
        except Exception as e:
            stats["errors"].append(f"Ошибка отправки обработчику: {e}")

        STATS["per_source"][source] = stats

        print(f"[{label}] Получено сообщений: {stats['messagesReceived']}")
        print(f"[{label}] Извлечено вакансий: {stats['jobsExtracted']}")
        print(f"[{label}] Добавлено новых строк: {stats['rowsAdded']}")
        print(f"[{label}] Дубликатов: {stats['duplicates']}")
        for err in stats["errors"]:
            print(f"[{label}] Ошибка: {err}", file=sys.stderr)

        memory = (
            f"Source {label}: received {stats['messagesReceived']} messages, "
            f"extracted {stats['jobsExtracted']} jobs, added {stats['rowsAdded']} rows, "
            f"{stats['duplicates']} duplicates."
        )
        return ActionResult(extracted_content=memory, long_term_memory=memory)

    # Подключаемся к запущенному видимому Chrome (текущий профиль).
    browser_session = BrowserSession(
        cdp_url=cdp_url, headless=False, highlight_elements=False
    )

    # Проверяем активную сессию Telegram Web ДО любых действий.
    # Если авторизация слетела — ничего не делаем, выходим с ошибкой.
    await browser_session.start()
    print("[collect] Проверка активной сессии Telegram Web...")
    if not await _telegram_authorized(browser_session):
        await browser_session.close()
        die(
            "В web.telegram.org НЕТ активной сессии Telegram Web (авторизация слетела "
            "или выполнен выход). Инструмент ничего не собирает. Зайдите в Telegram Web "
            "вручную в своём профиле, убедитесь, что сессия активна, и запустите снова."
        )

    llm = ChatOpenAI(
        model=OPENROUTER_MODEL,
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://github.com/telegram-jobs-collector",
            "X-Title": "Telegram Jobs Collector",
        },
    )

    task = build_task()
    print("[collect] Запуск Browser Use агента...")
    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser_session,
        controller=controller,
        use_vision=False,
        max_failures=3,
    )

    try:
        await agent.run(max_steps=max(60, len(CHANNELS) * 10))
    except Exception as e:
        print(f"[collect] Агент завершился с ошибкой: {e}", file=sys.stderr)
    finally:
        try:
            await browser_session.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Итоговая статистика
# --------------------------------------------------------------------------- #
def print_summary() -> None:
    processed = len(CHANNELS)
    done = STATS["per_source"]
    success = errored = 0
    total_msgs = total_jobs = total_added = total_dups = 0

    for ch in CHANNELS:
        st = done.get(ch)
        if st and not st.get("errors"):
            success += 1
            total_msgs += st.get("messagesReceived", 0)
            total_jobs += st.get("jobsExtracted", 0)
            total_added += st.get("rowsAdded", 0)
            total_dups += st.get("duplicates", 0)
        else:
            errored += 1

    print()
    print("=" * 40)
    print("Итоговая статистика")
    print("=" * 40)
    print(f"Обработано источников: {processed}")
    print(f"Успешно: {success}")
    print(f"С ошибками: {errored}")
    print(f"Всего сообщений: {total_msgs}")
    print(f"Всего вакансий: {total_jobs}")
    print(f"Добавлено строк: {total_added}")
    print(f"Дубликатов: {total_dups}")
    print(f"CSV: {os.path.abspath(CSV_PATH)}")


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        die(f"Не найден файл источников: {CHANNELS_FILE}")
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        die(f"Не удалось прочитать channels.json: {e}")
    if not isinstance(data, list):
        die("channels.json должен содержать JSON-массив ссылок")
    return [str(u).strip() for u in data if str(u).strip()]


CHANNELS: list[str] = []


async def main() -> None:
    global CHANNELS
    print("[@] Запуск Telegram Jobs Collector")

    if not OPENROUTER_API_KEY:
        die(
            "OPENROUTER_API_KEY не задан или пуст. Укажите ключ в файле .env "
            "(OPENROUTER_API_KEY=sk-or-...)."
        )

    CHANNELS = load_channels()
    if not CHANNELS:
        die("Список источников пуст (channels.json).")
    print(f"[@] Источников для обработки: {len(CHANNELS)}")

    server_proc = start_server()
    try:
        cdp_url = resolve_cdp_url()
        await run_agent(cdp_url)
    finally:
        stop_server(server_proc)
        stop_chrome()

    print_summary()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[collect] Прервано пользователем.")
        stop_chrome()
        sys.exit(130)
