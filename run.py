"""CLI для Telegram Jobs Collector.

Команды
-------
  python run.py            Запуск полного сбора через браузер (collect.py):
                           Browser Use + Browser Harness + Telegram Web.
                           Требует запущенный Google Chrome с удалённой отладкой
                           и активной сессией Telegram Web.

  python run.py --demo     Прогон реального конвейера обработки на примерах
                           сообщений (живой OpenRouter). Гарантированно создаёт
                           telegram.csv с извлечёнными вакансиями без браузера.
                           Удобно для проверки, что всё работает end-to-end.

  python run.py --selftest Запуск встроенных проверок ядра (lib + server).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lib

lib.setup_console()


def run_full() -> None:
    """Полный сбор через браузер (collect.py)."""
    import asyncio

    import collect

    asyncio.run(collect.main())


def run_selftest() -> None:
    import test_lib  # noqa: F401  (скрипт сам делает assert/exit при ошибке)


# --------------------------------------------------------------------------- #
# Demo-режим: реальный конвейер на примерах сообщений
# --------------------------------------------------------------------------- #
def _sample_messages(channel: str, idx: int) -> list[dict]:
    """Несколько реалистичных сообщений канала: подходящие вакансии + мусор.

    Ссылки уникальны для каждого канала, чтобы дедупликация не съедала
    повторы между источниками.
    """
    base = f"https://t.me/{channel}"
    return [
        {
            "messageId": f"{idx}01",
            "text": (
                f"🚀 Hiring: Senior Backend Engineer at {channel.title()} Labs "
                f"(Warsaw, Poland). Stack: Node.js, Nest.js, TypeScript. "
                f"Fully remote. Apply: https://careers.{channel}.dev/b/{idx}01"
            ),
            "publishedAt": "2026-07-11T08:15:00Z",
            "url": f"{base}/{idx}01",
            "links": [f"https://careers.{channel}.dev/b/{idx}01"],
        },
        {
            "messageId": f"{idx}02",
            "text": (
                f"We are looking for a Frontend Developer (React, TypeScript). "
                f"Location: Berlin, Germany. Hybrid. More: "
                f"https://jobs.{channel}.dev/fe-{idx}02"
            ),
            "publishedAt": "2026-07-11T09:00:00Z",
            "url": f"{base}/{idx}02",
            "links": [f"https://jobs.{channel}.dev/fe-{idx}02"],
        },
        {
            "messageId": f"{idx}03",
            "text": (
                f"Full-Stack Engineer wanted. Next.js + Express.js + JavaScript. "
                f"Remote. Details: https://apply.{channel}.dev/{idx}03"
            ),
            "publishedAt": "2026-07-11T10:30:00Z",
            "url": f"{base}/{idx}03",
            "links": [f"https://apply.{channel}.dev/{idx}03"],
        },
        {
            "messageId": f"{idx}04",
            "text": "Weekend sale! 30% off all electronics at our store. Come visit us.",
            "publishedAt": "2026-07-11T11:00:00Z",
            "url": f"{base}/{idx}04",
            "links": [],
        },
        {
            "messageId": f"{idx}05",
            "text": (
                f"AI Engineering role — building LLM agents. Python, PyTorch. "
                f"On-site in London. https://ai-co.{channel}.dev/{idx}05"
            ),
            "publishedAt": "2026-07-11T12:00:00Z",
            "url": f"{base}/{idx}05",
            "links": [f"https://ai-co.{channel}.dev/{idx}05"],
        },
        {
            "messageId": f"{idx}06",
            "text": "Marketing manager needed for our crypto project. Great salary. DM us.",
            "publishedAt": "2026-07-11T13:00:00Z",
            "url": f"{base}/{idx}06",
            "links": [],
        },
    ]


def _load_channels() -> list[str]:
    path = os.path.join(HERE, "channels.json")
    if not os.path.exists(path):
        print("ОШИБКА: channels.json не найден", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as e:
        print(f"ОШИБКА: не удалось прочитать channels.json: {e}", file=sys.stderr)
        sys.exit(1)
    return [str(u).strip() for u in data if str(u).strip()]


def run_demo() -> None:
    import server as server_mod

    port = int(os.getenv("PORT", "3006"))
    endpoint = f"http://127.0.0.1:{port}/import-telegram"
    csv_path = os.path.join(HERE, "telegram.csv")

    # Запускаем локальный обработчик как отдельный процесс.
    import subprocess

    env = os.environ.copy()
    env["PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py")],
        env=env,
        cwd=HERE,
    )
    try:
        # Ждём готовности.
        ok = False
        for _ in range(40):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                pass
            time.sleep(0.5)
        if not ok:
            print("ОШИБКА: локальный обработчик не запустился", file=sys.stderr)
            sys.exit(1)

        channels = _load_channels()
        stats_total = {
            "sources": len(channels),
            "success": 0,
            "errors": 0,
            "messages": 0,
            "jobs": 0,
            "added": 0,
            "duplicates": 0,
        }

        for i, src in enumerate(channels, 1):
            channel = lib.parse_telegram_source(src)[0] or f"channel{i}"
            messages = _sample_messages(channel, i)
            payload = json.dumps({"source": src, "messages": messages}).encode()
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                data = json.loads(e.read().decode())

            label = lib.parse_telegram_source(src)[0] or src
            print(f"[@{label}] Получено сообщений: {data.get('messagesReceived', 0)}")
            print(f"[@{label}] Извлечено вакансий: {data.get('jobsExtracted', 0)}")
            print(f"[@{label}] Добавлено новых строк: {data.get('rowsAdded', 0)}")
            print(f"[@{label}] Дубликатов: {data.get('duplicates', 0)}")
            for err in data.get("errors", []):
                print(f"[@{label}] Ошибка: {err}", file=sys.stderr)

            if data.get("errors"):
                stats_total["errors"] += 1
            else:
                stats_total["success"] += 1
            stats_total["messages"] += data.get("messagesReceived", 0)
            stats_total["jobs"] += data.get("jobsExtracted", 0)
            stats_total["added"] += data.get("rowsAdded", 0)
            stats_total["duplicates"] += data.get("duplicates", 0)

        print()
        print("=" * 40)
        print("Итоговая статистика (demo)")
        print("=" * 40)
        print(f"Обработано источников: {stats_total['sources']}")
        print(f"Успешно: {stats_total['success']}")
        print(f"С ошибками: {stats_total['errors']}")
        print(f"Всего сообщений: {stats_total['messages']}")
        print(f"Всего вакансий: {stats_total['jobs']}")
        print(f"Добавлено строк: {stats_total['added']}")
        print(f"Дубликатов: {stats_total['duplicates']}")
        print(f"CSV: {os.path.abspath(csv_path)}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Jobs Collector CLI")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--demo", action="store_true", help="Прогон конвейера на примерах (без браузера)"
    )
    group.add_argument(
        "--selftest", action="store_true", help="Встроенные проверки ядра"
    )
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    elif args.demo:
        run_demo()
    else:
        run_full()


if __name__ == "__main__":
    main()
