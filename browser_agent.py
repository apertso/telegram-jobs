"""Браузерный агент: OpenRouter tool-calling цикл поверх Playwright MCP.

Координатор (collect.py) запускает MCP-процесс (Playwright MCP через Extension)
и создаёт MCP-сессию. Для каждого источника этот модуль запускает отдельный
tool-calling цикл: модель выбирает разрешённые MCP tools, координатор
выполняет их, результат возвращается модели. Итог — строго разобранный JSON
с сообщениями текущего источника.

Playwright MCP подключается к текущему профилю Chrome ЧЕРЕЗ расширение,
новый браузер не запускается, remote debugging не используется, профиль не
читается, chrome.exe не завершается. Токен расширения доступен только MCP-процессу.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import lib

HERE = Path(__file__).resolve().parent

# Обязательный allowlist MCP tools. Любой tool вне списка запрещён.
ALLOWED_MCP_TOOLS = {
    "browser_tabs",
    "browser_navigate",
    "browser_snapshot",
    "browser_find",
    "browser_wait_for",
    "browser_click",
    "browser_press_key",
    "browser_evaluate",
    "browser_highlight",
    "browser_hide_highlight",
}

# Явно запрещённые tools.
FORBIDDEN_MCP_TOOLS = {"browser_run_code_unsafe"}

# Параметр прокрутки / ожидания по умолчанию.
HIGHLIGHT_BEFORE_CLICK = (os.getenv("HIGHLIGHT_BEFORE_CLICK", "false").lower()
                          not in ("0", "false", "no", "off"))
MAX_MESSAGE_TEXT = 8000
MAX_MESSAGES = int(os.getenv("TELEGRAM_MESSAGES_LIMIT", "30"))

# Параметры scroll-collect цикла (R4).
SCROLL_MAX_ITER = int(os.getenv("SCROLL_MAX_ITER", "15"))
SCROLL_WAIT_MS = int(os.getenv("SCROLL_WAIT_MS", "800"))
SCROLL_TAIL_TOLERANCE_S = int(os.getenv("SCROLL_TAIL_TOLERANCE_S", "60"))

# Read-only JS для извлечения сообщений из Telegram Web (web.telegram.org/k).
# НЕ изменяет DOM, storage, cookies или сеть. Только чтение видимых сообщений.
EXTRACT_MESSAGES_JS = """() => {
  const pad = n => String(n).padStart(2, '0');
  function toIso(ts) {
    const d = new Date(parseInt(ts, 10) * 1000);
    if (isNaN(d.getTime())) return '';
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth()+1) + '-' + pad(d.getUTCDate())
      + 'T' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + 'Z';
  }
  // Ищем элементы с data-mid (маркер сообщения в Telegram Web K).
  const nodes = Array.from(document.querySelectorAll('[data-mid]'));
  let items = [];
  for (const it of nodes) {
    if (it.classList.contains('service') || it.classList.contains('bubbles-date-group')) continue;
    const mid = it.getAttribute('data-mid');
    if (!mid) continue;
    const timeEl = it.querySelector('.time, [data-time]');
    const ts = timeEl ? (timeEl.getAttribute('data-time') || timeEl.getAttribute('datetime') || '') : '';
    const textEl = it.querySelector('.text') || it.querySelector('.bubble-content') || it;
    const text = (textEl.innerText || textEl.textContent || '').trim();
    if (!text || text.length < 2) continue;
    const links = Array.from(it.querySelectorAll('a'))
      .map(a => a.href)
      .filter(h => h && /^https?:/i.test(h));
    items.push({ mid, ts, text, links });
  }
  return JSON.stringify(items.map(m => ({
    messageId: m.mid || '',
    text: m.text,
    publishedAt: toIso(m.ts),
    url: '',
    links: m.links
  })));
}"""


# --------------------------------------------------------------------------- #
# Безопасное логирование
# --------------------------------------------------------------------------- #
def _safe_args(args: dict) -> dict:
    """Очищает tool arguments перед логированием: оставляем только простые
    скалярные значения, обрезаем длинные строки. Без чувствительных данных."""
    if not isinstance(args, dict):
        return {}
    out = {}
    for k, v in args.items():
        if isinstance(v, (str, int, float, bool)):
            val = str(v)
            if len(val) > 200:
                val = val[:200] + "…"
            out[k] = val
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


def log(*parts) -> None:
    print("[browser_agent]", *parts, file=sys.stderr)


# --------------------------------------------------------------------------- #
# MCP session wrapper
# --------------------------------------------------------------------------- #
class MCPError(Exception):
    pass


class MCPSession:
    """Обёртка над Playwright MCP stdio-сессией.

    Запускает `npx @playwright/mcp --config playwright-mcp.json` как дочерний
    процесс БЕЗ shell, передаёт PLAYWRIGHT_MCP_EXTENSION_TOKEN только в его
    окружение. Выполняет initialization и получает список tools.
    """

    def __init__(self, package: str, config_path: Path):
        self.package = package
        self.config_path = config_path
        self.tools: dict[str, dict] = {}
        self._session = None
        self._read = None
        self._write = None
        self._proc = None
        self._ctx = None

    async def start(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        token = os.getenv("PLAYWRIGHT_MCP_EXTENSION_TOKEN") or ""
        env = os.environ.copy()
        env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = token
        env.pop("PLAYWRIGHT_MCP_HOST", None)
        # НЕ задаём PWTEST_EXTENSION_USER_DATA_DIR: при его отсутствии MCP
        # использует defaultUserDataDirForChannel("chrome") для проверки
        # установки расширения, но НЕ передаёт --user-data-dir в spawn.
        # В результате connect.html откроется в УЖЕ запущенном экземпляре Chrome
        # (т.к. spawn вызывается без --user-data-dir), и расширение подключится
        # к реальным вкладкам с активной сессией Telegram Web.
        env.pop("PWTEST_EXTENSION_USER_DATA_DIR", None)

        if not _validate_package(self.package):
            raise MCPError(f"Недопустимый PLAYWRIGHT_MCP_PACKAGE: {self.package!r}")

        params = StdioServerParameters(
            command="npx",
            args=["-y", self.package, "--config", str(self.config_path), "--extension"],
            env=env,
            # shell=False — не используем оболочку.
        )

        self._ctx = stdio_client(params)
        self._read, self._write = await self._ctx.__aenter__()
        self._session = ClientSession(self._read, self._write)
        await self._session.__aenter__()
        init = await self._session.initialize()
        log("MCP initialization:", init.serverInfo.name if init.serverInfo else "?")
        await self._load_tools()

    async def _load_tools(self) -> None:
        resp = await self._session.list_tools()
        self.tools = {}
        names = []
        for t in resp.tools:
            schema = t.inputSchema or {}
            self.tools[t.name] = {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": schema,
            }
            names.append(t.name)
        log("MCP tools:", ", ".join(names))

    async def verify_tools(self, require_highlight: bool = True) -> None:
        # Обязательные базовые tools (всегда необходимы).
        core = ALLOWED_MCP_TOOLS - {"browser_highlight", "browser_hide_highlight"}
        missing = sorted(core - set(self.tools))
        if missing:
            raise MCPError(f"Отсутствуют обязательные MCP tools: {missing}")
        # Highlight tools обязательны только при HIGHLIGHT_BEFORE_CLICK=true.
        if require_highlight:
            hl = {"browser_highlight", "browser_hide_highlight"} & set(self.tools)
            if len(hl) < 2:
                raise MCPError(
                    "HIGHLIGHT_BEFORE_CLICK=true, но MCP не предоставляет "
                    "browser_highlight/browser_hide_highlight."
                )
        # FORBIDDEN_MCP_TOOLS (например browser_run_code_unsafe) НЕ должны
        # быть доступны агенту: они исключены из tool_defs и блокируются в call().
        # Сам факт их наличия в сервере не прерывает запуск — мы просто не вызываем.

    async def call(self, name: str, arguments: dict, timeout: float = 30.0) -> str:
        """Выполняет MCP tool, возвращает текстовый результат.

        Перед выполнением проверяет allowlist и запрещённые tools.
        Таймаут по умолчанию 30с — предотвращает зависание.
        """
        if name in FORBIDDEN_MCP_TOOLS:
            raise MCPError(f"Tool {name} явно запрещён")
        if name not in ALLOWED_MCP_TOOLS:
            raise MCPError(f"Tool {name} не входит в allowlist")
        if name not in self.tools:
            raise MCPError(f"Tool {name} недоступен в MCP")
        log("MCP tool:", name, _safe_args(arguments))
        result = await asyncio.wait_for(
            self._session.call_tool(name, arguments or {}), timeout=timeout
        )
        return _mcp_result_to_text(result)

    async def stop(self) -> None:
        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._ctx is not None:
                await self._ctx.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass


def _mcp_result_to_text(result) -> str:
    parts = []
    content_list = getattr(result, "content", None)
    if isinstance(content_list, list):
        for item in content_list:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
    if not parts:
        parts.append(str(result))
    return "\n".join(parts)


def _validate_package(package: str) -> bool:
    """Разрешаем только известный scoped-пакет @playwright/mcp (без shell-метасимволов)."""
    if not package:
        return False
    if " " in package or ";" in package or "&" in package or "|" in package:
        return False
    return bool(re.fullmatch(r"@playwright/mcp(@[0-9][\w.\-]*)?", package))


# --------------------------------------------------------------------------- #
# Агентный цикл для одного источника
# --------------------------------------------------------------------------- #
NO_SESSION_MSG = (
    "Активная сессия Telegram Web не найдена. "
    "Выполните вход в Telegram в текущем профиле Chrome."
)


# --------------------------------------------------------------------------- #
# Парсинг результата browser_evaluate
# --------------------------------------------------------------------------- #
def _parse_evaluate_result(raw: str, source_url: str) -> list[dict]:
    """Парсит результат browser_evaluate.

    MCP возвращает результат в формате:
        ### Result
        <JSON>
        ### Ran Playwright code
        ...
    Извлекаем JSON между ### Result и ### Ran Playwright code.
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()

    # Извлекаем содержимое между "### Result" и "### Ran"
    if "### Result" in text:
        start = text.index("### Result") + len("### Result")
        end = text.find("### Ran", start)
        if end == -1:
            end = len(text)
        text = text[start:end].strip()

    # Убираем markdown-заборы.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        arr = json.loads(text)
        # MCP может обернуть JSON-массив в строку: json.loads даёт строку.
        if isinstance(arr, str):
            arr = json.loads(arr)
    except Exception:
        s = text.find("[")
        e = text.rfind("]")
        if s != -1 and e != -1 and e > s:
            try:
                arr = json.loads(text[s : e + 1])
                if isinstance(arr, str):
                    arr = json.loads(arr)
            except Exception:
                return []
        else:
            return []
    if not isinstance(arr, list):
        return []
    return _clean_messages({"messages": arr}, source_url)


def _clean_messages(data: dict, source_url: str) -> list[dict]:
    """Строгая фильтрация сообщений: только с текстом и корректными полями.
    Удаляет дубликаты по messageId, сохраняет порядок старых->новых."""
    raw = data.get("messages")
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("messageId") or "").strip()
        text = (m.get("text") or "").strip()
        if not text:
            continue
        text = text[:MAX_MESSAGE_TEXT]
        links = m.get("links") or []
        if not isinstance(links, list):
            links = []
        links = _clean_links(links, text)
        url = str(m.get("url") or "").strip()
        if not url:
            url = lib.message_permalink(source_url, mid)
        pub = str(m.get("publishedAt") or "").strip()
        key = mid or text
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "messageId": mid,
            "text": text,
            "publishedAt": pub,
            "url": url,
            "links": links,
        })
    return out


def _clean_links(links: list, text: str) -> list[str]:
    """Из links удаляет дубликаты, картинки/emoji, служебные Telegram-ссылки
    и ссылки, не относящиеся к сообщению."""
    TELEGRAM = ("t.me", "telegram.me", "web.telegram.org")
    keep = []
    seen = set()
    for l in links:
        if not isinstance(l, str):
            continue
        u = l.strip()
        if not re.match(r"^https?://", u, re.I):
            continue
        low = u.lower()
        if low in seen:
            continue
        if re.search(r"\.(png|jpg|jpeg|gif|webp|svg|bmp)(\?|$)", low):
            continue
        host = urlsplit(u).netloc.lower()
        if any(host == h or host.endswith("." + h) for h in TELEGRAM):
            path = urlsplit(u).path
            if not re.fullmatch(r"/[^/]+/\d+", path):
                continue
        seen.add(low)
        keep.append(u)
    return keep


# --------------------------------------------------------------------------- #
# R4: Scroll-collect — сбор сообщений в окне TELEGRAM_SINCE_HOURS
# --------------------------------------------------------------------------- #
def _parse_iso_ts(s: str) -> float | None:
    """Разбирает ISO 8601 метку времени в epoch-секунды (UTC).

    Возвращает None для пустой/некорректной строки. Поддерживает суффикс 'Z'
    и смещения часового пояса.
    """
    if not s:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _detect_scroll_direction(batch: list[dict]) -> tuple[str, str]:
    """Определяет, в какую сторону скроллить к старым/новым сообщениям.

    Сравнивает publishedAt первого и последнего сообщения в DOM-порядке.
    Возвращает (older_edge, newer_edge) — каждое 'top' или 'bottom'.
    Если временные метки отсутствуют или равны — по умолчанию ('top', 'bottom'),
    что соответствует стандартному расположению чата (старые сверху).
    """
    if len(batch) < 2:
        return "top", "bottom"
    first_ts = _parse_iso_ts(batch[0].get("publishedAt", ""))
    last_ts = _parse_iso_ts(batch[-1].get("publishedAt", ""))
    if first_ts is None or last_ts is None:
        return "top", "bottom"
    if first_ts <= last_ts:
        return "top", "bottom"
    return "bottom", "top"


def _scroll_js(direction: str) -> str:
    """Возвращает read-only JS для прокрутки контейнера сообщений.

    direction='top' — scrollTop=0 (к старым, если старые сверху).
    direction='bottom' — scrollTop=scrollHeight (к новым).
    """
    if direction == "bottom":
        return ("() => { const el = document.querySelector('.bubbles-scrollable') "
                "|| document.querySelector('[class*=\"scroll\"]'); "
                "if (el) el.scrollTop = el.scrollHeight; }")
    return ("() => { const el = document.querySelector('.bubbles-scrollable') "
            "|| document.querySelector('[class*=\"scroll\"]'); "
            "if (el) el.scrollTop = 0; }")


def _merge_by_id(accumulated: dict[str, dict], batch: list[dict]) -> int:
    """Сливает batch в accumulated (по messageId), возвращает кол-во новых."""
    new_count = 0
    for m in batch:
        mid = m.get("messageId") or ""
        key = mid or m.get("text", "")[:100]
        if key not in accumulated:
            accumulated[key] = m
            new_count += 1
    return new_count


def _newest_ts(batch: list[dict]) -> float | None:
    """Самая свежая метка publishedAt в batch (или None)."""
    best: float | None = None
    for m in batch:
        ts = _parse_iso_ts(m.get("publishedAt", ""))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def _oldest_ts(batch: list[dict]) -> float | None:
    """Самая старая метка publishedAt в batch (или None)."""
    best: float | None = None
    for m in batch:
        ts = _parse_iso_ts(m.get("publishedAt", ""))
        if ts is not None and (best is None or ts < best):
            best = ts
    return best


def _accumulated_oldest_ts(accumulated: dict[str, dict]) -> float | None:
    """Самая старая метка publishedAt во всём накопленном множестве."""
    best: float | None = None
    for m in accumulated.values():
        ts = _parse_iso_ts(m.get("publishedAt", ""))
        if ts is not None and (best is None or ts < best):
            best = ts
    return best


def _has_timestamps(messages: list[dict]) -> bool:
    """Проверяет, есть ли валидные метки publishedAt хотя бы в половине сообщений.

    Отвергает нереалистичные метки (epoch 0, дальнее будущее).
    """
    if not messages:
        return False
    now = time.time()
    ten_years_ago = now - 10 * 365 * 24 * 3600
    valid = sum(
        1 for m in messages
        if (ts := _parse_iso_ts(m.get("publishedAt", ""))) is not None
        and ten_years_ago < ts <= now + 3600
    )
    return valid >= len(messages) // 2


async def collect_with_scroll(
    mcp: "MCPSession",
    source_url: str,
    since_hours: int,
    max_iter: int = SCROLL_MAX_ITER,
    wait_ms: int = SCROLL_WAIT_MS,
    tail_tolerance_s: int = SCROLL_TAIL_TOLERANCE_S,
    extract_js: str | None = None,
    heal_callback=None,
) -> list[dict]:
    """Сбор сообщений из Telegram Web с прокруткой в обе стороны (R4).

    Telegram может открыть канал на последних сообщениях, на первом непрочи-
    танном, или в глубине истории. Этот цикл:
      1. Определяет направление к новым/старым сообщениям по publishedAt.
      2. Фаза 1 — скроллит к новым, пока не достигнет «живого хвоста»
         (новейшая метка в пределах tail_tolerance_s от now) или пока не
         перестанут появляться новые сообщения.
      3. Фаза 2 — скроллит к старым, пока не пройдёт cutoff
         (now - since_hours*3600) или пока не перестанут появляться новые.
      4. Сливает все уникальные сообщения, сортирует старые→новые.

    Если после начального извлечения сообщения есть, но метки времени
    отсутствуют, вызывается heal_callback (если передан) для генерации
    нового JS с актуальными селекторами. Этот JS используется во всех
    последующих извлечениях.

    Возвращает список сообщений; фильтр по времени применяет server.py.
    """
    js = extract_js or EXTRACT_MESSAGES_JS

    cutoff = time.time() - since_hours * 3600
    now = time.time()
    accumulated: dict[str, dict] = {}

    async def _extract() -> list[dict]:
        raw = await mcp.call("browser_evaluate", {"function": js}, timeout=10)
        return _parse_evaluate_result(raw, source_url)

    async def _scroll(direction: str) -> None:
        await mcp.call("browser_evaluate", {"function": _scroll_js(direction)}, timeout=8)
        await asyncio.sleep(wait_ms / 1000)

    # --- Начальное извлечение и определение направления ---
    batch = await _extract()
    _merge_by_id(accumulated, batch)

    # --- Auto-healing: если сообщения есть, но метки времени отсутствуют ---
    if batch and not _has_timestamps(batch) and heal_callback is not None:
        log("timestamps missing after initial extract, invoking heal_callback")
        healed_js = await heal_callback(mcp)
        if healed_js:
            js = healed_js
            log("heal_callback returned new JS, re-extracting")
            batch = await _extract()
            accumulated.clear()
            _merge_by_id(accumulated, batch)

    older_edge, newer_edge = _detect_scroll_direction(batch) if batch else ("top", "bottom")

    # --- Фаза 1: к новым сообщениям (достигаем «живого хвоста») ---
    no_ts_iter1 = 0
    for i in range(max_iter):
        if not batch:
            break
        nts = _newest_ts(batch)
        if nts is not None and (now - nts) <= tail_tolerance_s:
            log(f"scroll phase1 iter={i}: newest={nts:.0f} within tail tolerance, stop")
            break
        if nts is None:
            no_ts_iter1 += 1
            if no_ts_iter1 > 3:
                log(f"scroll phase1 iter={i}: no timestamps after {no_ts_iter1} iterations, stop")
                break
        await _scroll(newer_edge)
        batch = await _extract()
        new_count = _merge_by_id(accumulated, batch)
        if new_count == 0:
            log(f"scroll phase1 iter={i}: no new messages, stop")
            break

    # --- Фаза 2: к старым сообщениям (до cutoff или дна истории) ---
    batch = await _extract()
    acc_oldest_prev: float | None = None
    no_ts_iter = 0
    for i in range(max_iter):
        _merge_by_id(accumulated, batch)

        # Проверяем accumulated set, а не только текущий batch: Telegram
        # виртуализирует DOM, и старые сообщения могут быть вытеснены из
        # видимой области, но мы их уже собрали.
        acc_oldest = _accumulated_oldest_ts(accumulated)

        if acc_oldest is not None:
            if acc_oldest < cutoff:
                log(f"scroll phase2 iter={i}: acc_oldest={acc_oldest:.0f} < cutoff={cutoff:.0f}, stop")
                break
            # Progress check: если accumulated oldest не уменьшился после
            # прокрутки — мы не загружаем ничего старше, стоп.
            if acc_oldest_prev is not None and acc_oldest >= acc_oldest_prev:
                log(f"scroll phase2 iter={i}: no progress (oldest={acc_oldest:.0f} >= prev={acc_oldest_prev:.0f}), stop")
                break
            acc_oldest_prev = acc_oldest
        else:
            # Нет валидных меток времени — не можем определить возраст.
            # Ограничиваем количество итераций, чтобы не уходить в глубину.
            no_ts_iter += 1
            if no_ts_iter > 3:
                log(f"scroll phase2 iter={i}: no timestamps after {no_ts_iter} iterations, stop")
                break

        if not batch:
            break
        await _scroll(older_edge)
        prev_size = len(accumulated)
        batch = await _extract()
        _merge_by_id(accumulated, batch)
        if len(accumulated) == prev_size:
            log(f"scroll phase2 iter={i}: no new messages after scroll, stop")
            break

    merged = list(accumulated.values())
    merged.sort(key=lambda m: (_parse_iso_ts(m.get("publishedAt", "")) or 0))
    return merged


# --------------------------------------------------------------------------- #
# Проверка активной Telegram-сессии через snapshot
# --------------------------------------------------------------------------- #
async def telegram_session_active(mcp: MCPSession, tab_hint: str) -> bool:
    """Через browser_snapshot определяет наличие активной Telegram-сессии.
    При QR/форме входа/отсутствии интерфейса — False."""
    try:
        snap = await mcp.call("browser_snapshot", {}, timeout=15)
    except Exception:
        return False
    if not snap.strip():
        return False
    lower = snap.lower()
    login_markers = [
        "auth-wrapper", "auth-pages", "login-form", "auth-phone-number",
        "qr-code", "#auth", "sign in",
    ]
    if any(m in lower for m in login_markers):
        log("session: login form detected")
        return False
    authed_markers = [
        "chatlist", "bubbles", "_chat-list", "column-left",
        "archived chats", "search", "saved messages",
        "contacts", "settings", "new chat",
        "chats", "telegram web",
    ]
    matched = [m for m in authed_markers if m in lower]
    if matched:
        log("session: active, markers:", matched)
        return True
    log("session: no markers, snap len:", len(snap), "preview:", snap[:300])
    return False


# --------------------------------------------------------------------------- #
# Управление вкладками
# --------------------------------------------------------------------------- #
def _parse_tabs(result: str) -> list[dict]:
    """Парсит результат browser_tabs в список словарей.

    Поддерживаемые форматы:
      - JSON-массив: [{"tabId": "1", "url": "...", "title": "..."}]
      - MCP-текстовый: "- 0: (current) [Title](url)" или "- 0: [Title](url)"
    """
    if not result or not result.strip():
        return []
    text = result.strip()

    if "### Result" in text:
        start = text.index("### Result") + len("### Result")
        end = text.find("### Ran", start)
        if end == -1:
            end = len(text)
        text = text[start:end].strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    tabs: list[dict] = []

    # Пытаемся распарсить как JSON.
    try:
        data = json.loads(text)
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        s = text.find("[")
        e = text.rfind("]")
        if s != -1 and e != -1 and e > s:
            try:
                data = json.loads(text[s : e + 1])
                if isinstance(data, str):
                    data = json.loads(data)
            except Exception:
                data = None
        else:
            data = None

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                tab_id = str(item.get("tabId") or item.get("tabid") or item.get("id") or "")
                url = str(item.get("url") or "")
                title = str(item.get("title") or "")
                if tab_id:
                    tabs.append({"tabId": tab_id, "url": url, "title": title})
        if tabs:
            return tabs

    # Текстовый формат MCP: "- 0: (current) [Title](url)" или "- 0: [Title](url)"
    for line in text.splitlines():
        line = line.strip()
        # Пропускаем пустые строки и заголовки разделов.
        if not line or line.startswith("#"):
            continue
        # Убираем начальный дефис/номер.
        line = re.sub(r"^[-*\d]+\s*\.?\s*", "", line)
        # Извлекаем tabId — это первое число до двоеточия.
        m = re.match(r"^(\S+?)\s*:", line)
        if not m:
            continue
        tab_id = m.group(1)
        # Извлекаем url из markdown-ссылки [Title](url) или [Title](url?...).
        url = ""
        m_url = re.search(r'\]\(([^)]+)\)', line)
        if m_url:
            url = m_url.group(1)
        # Извлекаем title из markdown-ссылки [Title](url).
        title = ""
        m_title = re.search(r'\[([^\]]+)\]\(', line)
        if m_title:
            title = m_title.group(1)
        # Если url не найден, ищем прямой URL в строке.
        if not url:
            m_url2 = re.search(r'(https?://\S+)', line)
            if m_url2:
                url = m_url2.group(1)
        tabs.append({"tabId": tab_id, "url": url, "title": title})

    return tabs


async def list_tabs(mcp: "MCPSession") -> list[dict]:
    """Возвращает список вкладок: [{tabId, url, title}, ...]."""
    try:
        result = await mcp.call("browser_tabs", {"action": "list"}, timeout=10)
        return _parse_tabs(result)
    except Exception as e:
        log("list_tabs failed:", e)
        return []


async def close_tab(mcp: "MCPSession", tab_id: str) -> bool:
    """Закрывает вкладку по tabId. Возвращает True при успехе."""
    try:
        await mcp.call("browser_tabs", {"action": "close", "tabId": tab_id}, timeout=10)
        return True
    except Exception as e:
        log(f"close_tab {tab_id} failed:", e)
        return False


async def close_script_tabs(mcp: "MCPSession", tab_ids: set[str]) -> int:
    """Закрывает вкладки, открытые скриптом. Возвращает количество закрытых."""
    closed = 0
    for tab_id in tab_ids:
        if await close_tab(mcp, tab_id):
            closed += 1
    return closed
