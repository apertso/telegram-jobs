"""Collect recent Telegram Web messages through Playwright MCP.

Последовательность работы:
  1. Загружает настройки из .env и источники из channels.json.
  2. Запускает локальный HTTP-обработчик (server.py).
  3. Запускает локальный `@playwright/mcp` как дочерний stdio-процесс, подключённый
     к текущему профилю Chrome ЧЕРЕЗ Playwright Extension (без запуска нового
     Chrome, без remote debugging, без чтения профиля, без завершения chrome.exe).
  4. Python MCP-клиент проверяет минимальный allowlist. Токен расширения
     передаётся изолированному MCP-процессу без ключа OpenRouter.
  5. Через browser_tabs создаёт/находит рабочую вкладку и работает только в ней.
  6. Через snapshot проверяет активную Telegram-сессию (без входа/2FA).
  7. Для каждого источника browser_agent выполняет фиксированное read-only
     извлечение; сгенерированный моделью JavaScript не используется.
  8. Сообщения отправляются на /import-telegram.
  9. Ошибка одного источника не останавливает остальные.
 10. После завершения закрываются вкладки, открытые скриптом (connect.html),
     MCP-соединение, MCP-процесс и локальный сервер.
     Chrome и пользовательские вкладки НЕ закрываются.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import lib
import browser_agent as ba
import diagnostics

lib.setup_console()

HERE = Path(__file__).resolve().parent
LOG_PATH = str(HERE / "collect.log")
lib.setup_file_logging(LOG_PATH)
CSV_PATH = os.getenv("TELEGRAM_CSV", str(HERE / "telegram.csv"))
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = lib.get_openrouter_model()
PORT = int(os.getenv("PORT", "3000"))
SINCE_HOURS = int(os.getenv("TELEGRAM_SINCE_HOURS", "24"))
CHANNELS_FILE = HERE / "channels.json"
SERVER_FILE = HERE / "server.py"
MCP_CONFIG = HERE / "playwright-mcp.json"
ENDPOINT = f"http://127.0.0.1:{PORT}/import-telegram"
OPENROUTER_WORKERS = 2
SUBMIT_HTTP_TIMEOUT = 90
SUBMIT_TASK_TIMEOUT = 100
SERVER_AUTH_TOKEN = ""

STATS: dict = {"per_source": {}}

def die(msg: str) -> None:
    print("ОШИБКА:", msg, file=sys.stderr)
    sys.exit(1)


def source_label(url: str) -> str:
    return lib.source_label(url)


NO_MESSAGES_SKIP = "Нет сообщений на странице"


def _log_collection_message(
    source_url: str,
    message: dict,
    outcome: str,
    reason_code: str,
    reason: str,
    **extra,
) -> None:
    diagnostics.write_log("messages", {
        "source": source_url,
        "stage": "collection",
        "outcome": outcome,
        "reasonCode": reason_code,
        "reason": reason,
        "messageRef": diagnostics.message_ref(source_url, message),
        "message": message,
        **extra,
    })


def _empty_source_stats(source_url: str) -> dict:
    return {
        "source": source_url,
        "messagesReceived": 0,
        "filteredByTime": 0,
        "filteredByPrefilter": 0,
        "modelRequests": 0,
        "jobsExtracted": 0,
        "jobsRejected": 0,
        "rowsAdded": 0,
        "duplicates": 0,
        "skipped": "",
        "errors": [],
    }


def _source_outcome(stats: dict) -> str:
    """Классификация результата обработки одного источника для итоговой статистики."""
    if stats.get("errors"):
        return "errored"
    if stats.get("skipped") == NO_MESSAGES_SKIP:
        return "empty"
    if stats.get("skipped"):
        return "skipped"
    return "success"


# --------------------------------------------------------------------------- #
# Локальный сервер
# --------------------------------------------------------------------------- #
def start_server() -> "subprocess.Popen":  # noqa: F821
    import subprocess

    global SERVER_AUTH_TOKEN
    SERVER_AUTH_TOKEN = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env["COLLECT_LOG_PATH"] = LOG_PATH
    env["TELEGRAM_JOBS_SERVER_TOKEN"] = SERVER_AUTH_TOKEN
    if diagnostics.enabled():
        env["AI_DIAGNOSTICS_RUN_ID"] = diagnostics.ensure_run_id()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_FILE)],
        env=env,
        cwd=str(HERE),
    )
    url = f"http://127.0.0.1:{PORT}/health"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {SERVER_AUTH_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=1.0) as r:
                if r.status == 200:
                    print(f"[collect] Локальный обработчик готов (порт {PORT})")
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    die("Локальный обработчик (server.py) не запустился вовремя.")


def stop_server(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception:
        pass
    # atexit в server.py не сработает при terminate() — удаляем lock явно.
    lock_path = CSV_PATH + ".lock"
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass


def submit_messages(
    source: str,
    messages: list[dict],
    timeout: int = SUBMIT_HTTP_TIMEOUT,
) -> dict:
    if not SERVER_AUTH_TOKEN:
        raise RuntimeError("Локальный обработчик не аутентифицирован")
    payload = json.dumps({"source": source, "messages": messages}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={
            "Authorization": f"Bearer {SERVER_AUTH_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # noqa: F821
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"errors": [f"HTTP {e.code}"]}
    except Exception as e:
        return {"errors": [f"Ошибка отправки обработчику: {e}"]}


async def _submit_collected_messages(source_url: str, messages: list[dict], stats: dict) -> None:
    """Отправляет собранные сообщения локальному server.py и обновляет STATS."""
    label = source_label(source_url)
    scraped_count = len(messages)
    try:
        t0 = time.monotonic()
        print(f"[{label}] Извлечение вакансий: {scraped_count} сообщений → обработчик...")
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                submit_messages,
                source_url,
                messages,
                SUBMIT_HTTP_TIMEOUT,
            ),
            timeout=SUBMIT_TASK_TIMEOUT,
        )
        print(f"[{label}] Извлечение завершено за {time.monotonic() - t0:.1f}s")
        stats.update({
            "messagesReceived": resp.get("messagesReceived", scraped_count),
            "filteredByTime": resp.get("filteredByTime", 0),
            "filteredByPrefilter": resp.get("filteredByPrefilter", 0),
            "modelRequests": resp.get("modelRequests", 0),
            "jobsExtracted": resp.get("jobsExtracted", 0),
            "jobsRejected": resp.get("jobsRejected", 0),
            "rowsAdded": resp.get("rowsAdded", 0),
            "duplicates": resp.get("duplicates", 0),
            "skipped": resp.get("skipped", ""),
            "errors": stats["errors"] + resp.get("errors", []),
        })
    except Exception as e:
        stats["errors"].append(f"Ошибка отправки: {e}")

    STATS["per_source"][source_url] = stats
    _print_source_stats(source_url, stats)


async def _openrouter_worker(_worker_id: int, queue: asyncio.Queue) -> None:
    """Последовательно обрабатывает задачи отправки; несколько worker'ов идут параллельно."""
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            source_url, messages, stats = item
            await _submit_collected_messages(source_url, messages, stats)
        finally:
            queue.task_done()


def _print_source_stats(source_url: str, stats: dict) -> None:
    label = source_label(source_url)
    print(
        f"[{label}] Собрано сообщений: {stats['messagesReceived']} "
        f"(отфильтровано по времени: {stats.get('filteredByTime', 0)}, "
        f"prefilter: {stats.get('filteredByPrefilter', 0)})"
    )
    print(
        f"[{label}] Вакансий: {stats['jobsExtracted']} "
        f"(отклонено: {stats.get('jobsRejected', 0)}), "
        f"строк: {stats['rowsAdded']}, дублей: {stats['duplicates']}"
    )
    if stats.get("skipped"):
        print(f"[{label}] Пропущен: {stats['skipped']}")
    for err in stats["errors"]:
        print(f"[{label}] Ошибка: {err}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Обработка одного источника
# --------------------------------------------------------------------------- #
async def process_source(
    mcp: ba.MCPSession,
    source_url: str,
    submit_queue: asyncio.Queue | None = None,
    *,
    check_session: bool = True,
) -> dict:
    label = source_label(source_url)
    print(f"[{label}] Обработка источника...")
    diagnostics.write_log("messages", {
        "source": source_url,
        "stage": "collection",
        "outcome": "started",
        "reasonCode": "collection_started",
        "reason": "started collecting source",
    })

    stats = _empty_source_stats(source_url)
    session_check_next = False

    try:
        # Навигация: about:blank → полный URL. Playwright не перезагружает
        # при смене только hash, поэтому сначала уходим на about:blank.
        print(f"[{label}] Навигация: {source_url}")
        try:
            await mcp.call("browser_navigate", {"url": "about:blank"}, timeout=10)
            await mcp.call("browser_navigate", {"url": source_url}, timeout=15)
        except Exception:
            session_check_next = True
            raise

        ready = await ba.wait_for_channel_ready(mcp)
        if not ready:
            session_check_next = True
            print(f"[{label}] DOM канала не готов после ожидания")

        # Проверка сессии: первый источник всегда, далее только после
        # timeout/ошибок навигации/пустого DOM.
        if (check_session or session_check_next) and not await ba.telegram_session_active(mcp):
            stats["errors"].append(ba.NO_SESSION_MSG)
            STATS["per_source"][source_url] = stats
            diagnostics.write_log("messages", {
                "source": source_url,
                "stage": "collection",
                "outcome": "error",
                "reasonCode": "telegram_session_inactive",
                "reason": ba.NO_SESSION_MSG,
            })
            return {"queued": False, "session_check_next": True}

        # Scroll-collect messages using the fixed read-only extractor.
        print(f"[{label}] Сбор с прокруткой (since_hours={SINCE_HOURS})...")
        messages = await ba.collect_with_scroll(mcp, source_url, SINCE_HOURS)

    except Exception as e:
        stats["errors"].append(f"{e}")
        STATS["per_source"][source_url] = stats
        diagnostics.write_log("messages", {
            "source": source_url,
            "stage": "collection",
            "outcome": "error",
            "reasonCode": "collection_error",
            "reason": str(e),
        })
        return {"queued": False, "session_check_next": True}

    scraped_count = len(messages)
    if scraped_count == 0:
        stats["skipped"] = NO_MESSAGES_SKIP
        STATS["per_source"][source_url] = stats
        diagnostics.write_log("messages", {
            "source": source_url,
            "stage": "collection",
            "outcome": "empty",
            "reasonCode": "no_messages_collected",
            "reason": NO_MESSAGES_SKIP,
        })
        print(f"[{label}] Собрано сообщений: 0")
        print(f"[{label}] Пропущен: {stats['skipped']}")
        return {"queued": False, "session_check_next": session_check_next}

    stats["messagesReceived"] = scraped_count
    for message in messages:
        _log_collection_message(
            source_url,
            message,
            "collected",
            "collected_from_browser",
            "normalized message collected from browser",
        )
    if submit_queue is None:
        await _submit_collected_messages(source_url, messages, stats)
    else:
        await submit_queue.put((source_url, messages, stats))
        print(f"[{label}] Сообщения поставлены в очередь OpenRouter: {scraped_count}")

    return {"queued": True, "session_check_next": session_check_next}


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


def _selector_to_source_url(selector: str) -> str:
    value = (selector or "").strip()
    if not value:
        return ""
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if value.startswith("@"):
        return f"https://web.telegram.org/k/#{value}"
    return f"https://web.telegram.org/k/#@{value}"


def select_channels(channels: list[str], selector: str) -> list[str]:
    """Filters configured channels to a single source selected by URL or @name."""
    target = _selector_to_source_url(selector)
    if not target:
        return channels
    target_url = lib.normalize_url(target)
    target_label = lib.source_label(target).casefold()
    selected = [
        channel for channel in channels
        if lib.normalize_url(channel) == target_url
        or lib.source_label(channel).casefold() == target_label
    ]
    if not selected:
        raise ValueError(f"Источник {selector!r} не найден в channels.json")
    return selected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Jobs Collector")
    parser.add_argument(
        "--channel",
        "--source",
        dest="channel",
        default=(os.getenv("TELEGRAM_ONLY_CHANNEL") or "").strip(),
        help="Обработать только один источник из channels.json: @name или полный URL.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    print("[@] Запуск Telegram Jobs Collector")

    if diagnostics.enabled():
        diagnostics.ensure_run_id()
        diagnostics.clean_start()
        print(f"[collect] AI diagnostics: {diagnostics.diagnostics_dir()}")

    if not OPENROUTER_API_KEY:
        die("OPENROUTER_API_KEY не задан или пуст. Укажите ключ в файле .env.")
    if not (os.getenv("PLAYWRIGHT_MCP_EXTENSION_TOKEN") or "").strip():
        die(
            "PLAYWRIGHT_MCP_EXTENSION_TOKEN не задан или пуст. "
            "Укажите токен расширения в файле .env."
        )

    try:
        channels = select_channels(load_channels(), args.channel)
    except ValueError as e:
        die(str(e))
    if not channels:
        die("Список источников пуст (channels.json).")
    print(f"[@] Источников для обработки: {len(channels)}")
    if args.channel:
        print(f"[@] Ограничение источника: {source_label(channels[0])}")

    server_proc = start_server()
    mcp = ba.MCPSession(MCP_CONFIG)
    tabs_at_start: list[dict] = []
    working_tab_id: str | None = None
    submit_queue: asyncio.Queue | None = None
    workers: list[asyncio.Task] = []
    try:
        await mcp.start()
        await mcp.verify_tools()
        print(f"[collect] MCP подключён к Playwright Extension, tools проверены")
        print(f"[collect] Модель извлечения: {OPENROUTER_MODEL}")
        # Снимок вкладок сразу после единственного MCP-старта (R2).
        tabs_at_start = await ba.list_tabs(mcp)
        working_tab_id = ba.current_tab_id(tabs_at_start)

        # Сброс CSV перед сбором — каждый прогон начинает с чистого файла.
        lib.reset_csv(CSV_PATH)
        print(f"[collect] CSV сброшен: {os.path.abspath(CSV_PATH)}")
        STATS["per_source"] = {src: _empty_source_stats(src) for src in channels}
        submit_queue = asyncio.Queue()
        workers = [
            asyncio.create_task(_openrouter_worker(i + 1, submit_queue))
            for i in range(OPENROUTER_WORKERS)
        ]

        # Обработка источников по одному — прямая навигация + evaluate.
        # Sources are intentionally processed sequentially in one active tab.
        session_check_required = True
        for src in channels:
            try:
                # Hard per-source timeout prevents a stalled browser session.
                result = await asyncio.wait_for(
                    process_source(
                        mcp,
                        src,
                        submit_queue,
                        check_session=session_check_required,
                    ),
                    timeout=120,
                )
                session_check_required = bool(result.get("session_check_next"))
            except asyncio.TimeoutError:
                label = source_label(src)
                print(f"[{label}] ТАЙМАУТ — источник пропущен (120с)", file=sys.stderr)
                stats = _empty_source_stats(src)
                stats["errors"].append("Таймаут обработки источника (120с)")
                STATS["per_source"][src] = stats
                diagnostics.write_log("messages", {
                    "source": src,
                    "stage": "collection",
                    "outcome": "error",
                    "reasonCode": "source_timeout",
                    "reason": "Таймаут обработки источника (120с)",
                })
                session_check_required = True

    finally:
        if submit_queue is not None and workers:
            await submit_queue.join()
            for _ in workers:
                await submit_queue.put(None)
            await asyncio.gather(*workers, return_exceptions=True)
        if tabs_at_start:
            tabs_final = await ba.list_tabs(mcp)
            script_tab_ids = ba.script_tab_ids_to_close(
                tabs_at_start, tabs_final, working_tab_id=working_tab_id,
            )
            if script_tab_ids:
                closed = await ba.close_script_tabs(mcp, script_tab_ids)
                print(f"[collect] Закрыто вкладок скрипта: {closed}")
        await mcp.stop()
        stop_server(server_proc)

    print_summary()


def print_summary() -> None:
    done = STATS["per_source"]
    success = errored = skipped = empty = 0
    total_msgs = total_jobs = total_added = total_dups = total_filtered = 0
    total_prefiltered = total_model_requests = total_rejected = 0
    for ch in done:
        st = done[ch]
        outcome = _source_outcome(st)
        if outcome == "errored":
            errored += 1
        elif outcome == "empty":
            empty += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            success += 1
        total_msgs += st.get("messagesReceived", 0)
        total_filtered += st.get("filteredByTime", 0)
        total_prefiltered += st.get("filteredByPrefilter", 0)
        total_model_requests += st.get("modelRequests", 0)
        total_jobs += st.get("jobsExtracted", 0)
        total_rejected += st.get("jobsRejected", 0)
        total_added += st.get("rowsAdded", 0)
        total_dups += st.get("duplicates", 0)

    print()
    print("=" * 40)
    print("Итоговая статистика")
    print("=" * 40)
    print(f"Обработано источников: {len(done)}")
    print(f"Успешно: {success}")
    print(f"Пропущено (нет свежих сообщений): {skipped}")
    print(f"Не собрано (0 сообщений на странице): {empty}")
    print(f"С ошибками: {errored}")
    print(f"Всего сообщений получено: {total_msgs}")
    print(f"Отфильтровано по времени: {total_filtered}")
    print(f"Отфильтровано prefilter: {total_prefiltered}")
    print(f"Запросов к модели: {total_model_requests}")
    print(f"Всего вакансий: {total_jobs}")
    print(f"Отклонено вакансий: {total_rejected}")
    print(f"Добавлено строк: {total_added}")
    print(f"Дубликатов: {total_dups}")
    print(f"CSV: {os.path.abspath(CSV_PATH)}")


# --------------------------------------------------------------------------- #
# Ctrl+C: корректное завершение дочерних процессов (без закрытия Chrome)
# --------------------------------------------------------------------------- #
def _handle_sigint(signum, frame) -> None:
    print("\n[collect] Прервано пользователем (Ctrl+C).")
    sys.exit(130)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[collect] Прервано пользователем.")
        sys.exit(130)
