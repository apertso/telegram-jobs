"""Проверка реального конвейера: server.py + lib.py + OpenRouter.

Запускает настоящий локальный обработчик и отправляет ему реалистичные
сообщения из Telegram-каналов (как это делает агент collect.py). Проверяет:
  - извлечение подходящих вакансий через живой OpenRouter;
  - фильтрацию по направлениям/стеку;
  - нормализацию WorkMode/URL;
  - дедупликацию;
  - запись реального telegram.csv.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import lib

lib.setup_console()

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

PORT = 3096
os.environ["PORT"] = str(PORT)
ENDPOINT = f"http://127.0.0.1:{PORT}/import-telegram"
SRC = "https://web.telegram.org/k/#@evacuatejobs"

# Реалистичные сообщения: подходящие вакансии + неподходящие (должны отсеяться).
MESSAGES = [
    {
        "messageId": "501",
        "text": "🚀 Hiring: Senior Backend Engineer at HOS247 (Warsaw, Poland). Stack: Node.js, Nest.js, TypeScript. Fully remote. Apply: https://careers.hos247.com/j/501",
        "publishedAt": "2026-07-11T08:15:00Z",
        "url": "https://t.me/evacuatejobs/501",
        "links": ["https://careers.hos247.com/j/501"],
    },
    {
        "messageId": "502",
        "text": "We are looking for a Frontend Developer (React, TypeScript). Location: Berlin, Germany. Hybrid. More: https://jobs.example.dev/fe-502",
        "publishedAt": "2026-07-11T09:00:00Z",
        "url": "https://t.me/evacuatejobs/502",
        "links": ["https://jobs.example.dev/fe-502"],
    },
    {
        "messageId": "503",
        "text": "Full-Stack Engineer wanted. Next.js + Express.js + JavaScript. Remote. Details: https://apply.startup.io/503",
        "publishedAt": "2026-07-11T10:30:00Z",
        "url": "https://t.me/evacuatejobs/503",
        "links": ["https://apply.startup.io/503"],
    },
    {
        "messageId": "504",
        "text": "Weekend sale! 30% off all electronics at our store. Come visit us on Saturday.",
        "publishedAt": "2026-07-11T11:00:00Z",
        "url": "https://t.me/evacuatejobs/504",
        "links": [],
    },
    {
        "messageId": "505",
        "text": "AI Engineering role — building LLM agents. Python, PyTorch. On-site in London. https://ai-co.example/505",
        "publishedAt": "2026-07-11T12:00:00Z",
        "url": "https://t.me/evacuatejobs/505",
        "links": ["https://ai-co.example/505"],
    },
    {
        "messageId": "506",
        "text": "Marketing manager needed for our crypto project. Great salary. DM us.",
        "publishedAt": "2026-07-11T13:00:00Z",
        "url": "https://t.me/evacuatejobs/506",
        "links": [],
    },
]


def post(obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(
        ENDPOINT, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main():
    # начинаем с чистого состояния для воспроизводимости
    csv_path = os.path.join(os.getcwd(), "telegram.csv")
    if os.path.exists(csv_path):
        os.remove(csv_path)

    print("[@] Запуск локального обработчика (server.py)...")
    proc = subprocess.Popen(
        [sys.executable, "server.py"], env=os.environ.copy(), cwd=os.getcwd()
    )
    try:
        # дождаться готовности
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        print("[@] Отправка реалистичных сообщений в обработчик...")
        st1 = post({"source": SRC, "messages": MESSAGES})
        print("Первая отправка:", json.dumps(st1[1], ensure_ascii=False))

        print("[@] Повторная отправка тех же сообщений (проверка дедупликации)...")
        st2 = post({"source": SRC, "messages": MESSAGES})
        print("Вторая отправка:", json.dumps(st2[1], ensure_ascii=False))

        csv_path = os.path.join(os.getcwd(), "telegram.csv")
        print("\n=== telegram.csv ===")
        with open(csv_path, encoding="utf-8") as f:
            print(f.read())

        # Проверки
        s1 = st1[1]
        # Ожидаем ровно 3 подходящие вакансии (Backend/Node, Frontend/React,
        # Full-Stack/Next). Роль AI Engineering (#505, Python/PyTorch) корректно
        # отсеяна — в целевом стеке нет Python/PyTorch. Реклама/маркетинг отсеяны.
        assert s1["jobsExtracted"] == 3, f"ожидалось 3 вакансии, получено {s1['jobsExtracted']}"
        assert s1["rowsAdded"] == 3, f"ожидалось 3 добавленных строки, получено {s1['rowsAdded']}"
        assert s1["errors"] == [], f"неожиданные ошибки: {s1['errors']}"
        s2 = st2[1]
        assert s2["rowsAdded"] == 0, f"при повторе не должно быть новых строк, получено {s2['rowsAdded']}"
        assert s2["duplicates"] == 3, f"при повторе ожидались дубликаты, получено {s2['duplicates']}"

        # В CSV НЕ должно быть AI/маркетинговой/рекламной роли.
        with open(csv_path, encoding="utf-8") as f:
            content = f.read()
            lines = [l for l in content.splitlines() if l.strip()][1:]  # без заголовка
        assert "ai-co.example/505" not in content, "AI-роль с чужим стеком попала в CSV!"
        assert "LLM agents" not in content, "AI-роль попала в CSV!"
        assert all("http" in l for l in lines), "в CSV попала строка без URL вакансии"

        print("\n[OK] Конвейер сервер+lib+OpenRouter работает: CSV создан, вакансии извлечены, дедупликация подтверждена.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
