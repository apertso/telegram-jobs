"""Локальный HTTP-обработчик и запросы к OpenRouter.

Принимает собранные сообщения с браузерного агента:
    POST http://127.0.0.1:<PORT>/import-telegram
    { "source": "...", "messages": [ {messageId, text, publishedAt, url, links}, ... ] }

Отправляет их в OpenRouter для извлечения подходящих вакансий, нормализует,
выполняет дедупликацию и добавляет новые строки в telegram.csv.

Отвечает статистикой:
    { "source", "messagesReceived", "jobsExtracted", "rowsAdded", "duplicates", "errors" }
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify

import lib

load_dotenv()
lib.setup_console()

HERE = Path(__file__).resolve().parent
CSV_PATH = os.getenv("TELEGRAM_CSV", str(HERE / "telegram.csv"))
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_MODEL = (os.getenv("OPENROUTER_MODEL") or "tencent/hy3:free").strip()
PORT = int(os.getenv("PORT", "3000"))

# Допустимые направления и стек для фильтрации.
ALLOWED_DIRECTIONS = [
    "Backend",
    "Frontend",
    "Full-Stack",
    "Software Engineering",
    "AI Engineering",
    "Forward Deployed Engineering",
]
TARGET_STACK = [
    "Node.js",
    "Express.js",
    "Nest.js",
    "Next.js",
    "React",
    "Angular",
    "TypeScript",
    "JavaScript",
]

# OpenRouter REST endpoint (OpenAI-совместимый).
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# OpenRouter-клиент (лениво, чтобы не падать при старте без сети)
# --------------------------------------------------------------------------- #
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=OPENROUTER_API_KEY or "missing",
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "https://github.com/telegram-jobs-collector",
                "X-Title": "Telegram Jobs Collector",
            },
        )
    return _client


# --------------------------------------------------------------------------- #
# Построение промпта
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "You are a precise job-posting extraction assistant for a recruitment pipeline. "
    "You receive Telegram messages that may contain job postings. "
    "Extract ONLY postings that match BOTH conditions:\n"
    f"  - Direction is one of: {', '.join(ALLOWED_DIRECTIONS)}.\n"
    f"  - Tech stack includes at least one of: {', '.join(TARGET_STACK)}.\n"
    "A single message may contain several matching postings — emit one object per posting.\n"
    "Return a JSON object with a single key 'jobs' containing an array. "
    "Each job object MUST have exactly these keys: "
    "'title', 'company', 'location', 'workMode', 'url'.\n"
    "Rules:\n"
    "- Do NOT invent data. If a field is missing, use an empty string.\n"
    "- Normalize 'workMode' to one of: 'Remote', 'Hybrid', 'On-site', or '' (empty).\n"
    "- For 'url', choose the BEST link for that posting by priority:\n"
    "    1) a direct job/apply URL found in the message's Links (http/https, not Telegram);\n"
    "    2) the Telegram post permalink given for that message (its 'permalink');\n"
    "    3) otherwise empty string.\n"
    "- 'location' and 'workMode' are stored separately; do NOT put work mode into location.\n"
    "If no postings match, return {\"jobs\": []}. Respond with JSON only, no markdown."
)


def _build_user_prompt(source: str, messages: list[dict]) -> str:
    lines = [
        f"Source: {source}",
        f"Messages ({len(messages)}):",
        "",
    ]
    for i, m in enumerate(messages, 1):
        mid = m.get("messageId") or "?"
        permalink = m.get("url") or lib.message_permalink(source, str(mid))
        text = (m.get("text") or "").strip()
        if len(text) > 4000:
            text = text[:4000] + " …[truncated]"
        links = m.get("links") or []
        if not isinstance(links, list):
            links = []
        lines.append(f"[{i}] id={mid} permalink={permalink}")
        lines.append(f"text: {text}")
        lines.append(f"links: {', '.join(str(x) for x in links) if links else '(none)'}")
        lines.append("")
    lines.append("Extract matching jobs as specified.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Парсинг ответа модели
# --------------------------------------------------------------------------- #
def _extract_json(text: str):
    text = (text or "").strip()
    # убираем markdown-заборы, если модель их добавила
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s : e + 1])
        raise


def _is_job_obj(obj) -> bool:
    return isinstance(obj, dict) and ("title" in obj or "Title" in obj)


def _call_openrouter(model: str, user_prompt: str):
    """Вызывает OpenRouter. При неподдерживаемом response_format повторяет без него."""
    client = _get_client()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    return resp.choices[0].message.content


def process_messages(source: str, messages: list[dict]) -> dict:
    """Извлекает вакансии из сообщений источника, нормализует, дедуплицирует
    и записывает новые в CSV. Возвращает словарь статистики."""
    stats = {
        "source": source,
        "messagesReceived": len(messages or []),
        "jobsExtracted": 0,
        "rowsAdded": 0,
        "duplicates": 0,
        "errors": [],
    }

    if not messages:
        return stats

    # 1. Запрос к OpenRouter.
    try:
        raw = _call_openrouter(OPENROUTER_MODEL, _build_user_prompt(source, messages))
    except Exception as e:  # ошибки сети / API
        stats["errors"].append(f"OpenRouter: {e}")
        return stats

    # 2. Парсинг ответа.
    try:
        data = _extract_json(raw)
    except Exception as e:
        stats["errors"].append(f"Ошибка разбора ответа OpenRouter: {e}")
        return stats

    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        stats["errors"].append("В ответе OpenRouter нет массива 'jobs'")
        return stats

    stats["jobsExtracted"] = sum(1 for j in jobs if _is_job_obj(j))

    # 3. Нормализация, выбор URL и дедупликация.
    to_add = []
    for job in jobs:
        if not _is_job_obj(job):
            continue
        # Выбор URL по приоритету (согласно ТЗ):
        #   1. прямая ссылка на вакансию/отклик (http(s), не Telegram);
        #   2. пермализация Telegram, возвращённая моделью (priority 2) — сохраняем как есть;
        #   3/4. если модель не вернул url — публичная ссылка канала или исходный Telegram Web.
        job_url = job.get("url") or job.get("URL") or ""
        if lib.is_direct_link(job_url):
            job["url"] = lib.normalize_url(job_url)
        elif job_url:
            job["url"] = lib.normalize_url(job_url)
        else:
            job["url"] = lib.choose_url(source, None, "")
        to_add.append(job)

    try:
        rows_added, duplicates = lib.add_jobs(CSV_PATH, to_add)
        stats["rowsAdded"] = rows_added
        stats["duplicates"] = duplicates
    except Exception as e:
        stats["errors"].append(f"Ошибка записи CSV: {e}")

    return stats


# --------------------------------------------------------------------------- #
# Flask-эндпоинты
# --------------------------------------------------------------------------- #
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "csv": CSV_PATH})


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok"})


@app.route("/import-telegram", methods=["POST"])
def import_telegram():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    source = (payload.get("source") or "").strip()
    messages = payload.get("messages") or []

    if not source:
        return jsonify(
            {
                "source": "",
                "messagesReceived": 0,
                "jobsExtracted": 0,
                "rowsAdded": 0,
                "duplicates": 0,
                "errors": ["Отсутствует поле 'source'"],
            }
        ), 400

    if not isinstance(messages, list):
        messages = []

    stats = process_messages(source, messages)
    code = 200 if not stats["errors"] else 207
    return jsonify(stats), code


def main():
    print(f"[server] Запуск обработчика на порту {PORT}")
    print(f"[server] CSV: {CSV_PATH}")
    if not OPENROUTER_API_KEY:
        print(
            "[server] ВНИМАНИЕ: OPENROUTER_API_KEY пустой — извлечение вакансий не сработает.",
            file=sys.stderr,
        )
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
