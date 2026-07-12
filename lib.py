"""CSV-хранилище, нормализация и дедупликация для telegram.csv.

Все функции чистые и не зависят от сети, поэтому легко тестируются.
Файл сохраняется в кодировке UTF-8 с корректным экранированием CSV.
"""

from __future__ import annotations

import csv
import re
from urllib.parse import urlsplit

# Порядок колонок CSV (заголовок).
CSV_HEADER = ["Title", "Company", "Location", "WorkMode", "URL"]

# Известные tracking-параметры, удаляемые из URL.
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
}

# Нормализация значений WorkMode (ключ — приведённая к нижнему регистру форма).
WORK_MODE_MAP = {
    # Remote
    "remote": "Remote",
    "remotely": "Remote",
    "fully remote": "Remote",
    "100% remote": "Remote",
    "remote work": "Remote",
    "work from home": "Remote",
    "wfh": "Remote",
    # Hybrid
    "hybrid": "Hybrid",
    "hybrid work": "Hybrid",
    # On-site
    "onsite": "On-site",
    "on-site": "On-site",
    "office-based": "On-site",
    "office based": "On-site",
}

# Хосты Telegram — не считаются «прямой» ссылкой на вакансию.
TELEGRAM_HOSTS = ("t.me", "telegram.me", "web.telegram.org")


# --------------------------------------------------------------------------- #
# Базовая очистка текста
# --------------------------------------------------------------------------- #
def _clean(value) -> str:
    """Удаляет лишние пробелы по краям и внутри строки."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_hyphens(value) -> str:
    """Нормализует дефисы: неразрывный дефис, «en», «em» -> обычный «-»,
    а также схлопывает повторяющиеся дефисы."""
    if value is None:
        return ""
    s = str(value)
    # Неразрывный дефис (U+2011), «en dash» (U+2013), «em dash» (U+2014) -> дефис.
    s = s.replace("‑", "-").replace("–", "-").replace("—", "-")
    # Схлопываем несколько подряд идущих дефисов в один.
    s = re.sub(r"-{2,}", "-", s)
    return s


def normalize_work_mode(value) -> str:
    """Приводит значение WorkMode к одному из допустимых:
    Remote / Hybrid / On-site / '' (пусто)."""
    s = _clean(value).lower()
    if not s:
        return ""
    if s in WORK_MODE_MAP:
        return WORK_MODE_MAP[s]
    # Частичные совпадения для надёжности.
    if "remote" in s:
        return "Remote"
    if "hybrid" in s:
        return "Hybrid"
    if "on-site" in s or "onsite" in s or "office" in s:
        return "On-site"
    return ""


# --------------------------------------------------------------------------- #
# Работа с Telegram-ссылками
# --------------------------------------------------------------------------- #
def parse_telegram_source(url: str):
    """Извлекает (channel, thread) из ссылки Telegram Web.

    Примеры:
      https://web.telegram.org/k/#@evacuatejobs            -> ("evacuatejobs", None)
      https://web.telegram.org/k/#@cyprusithr?thread=46685 -> ("cyprusithr", "46685")

    В web.telegram.org параметр thread находится в фрагменте (#...?thread=...),
    поэтому он не попадает в query и сохраняется как есть.
    """
    if not url:
        return (None, None)
    frag = urlsplit(url).fragment
    channel = None
    thread = None
    if "?" in frag:
        base, qs = frag.split("?", 1)
        channel = base.lstrip("@")
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                if k == "thread":
                    thread = v
    else:
        channel = frag.lstrip("@")
    channel = channel or None
    thread = thread or None
    return (channel, thread)


def to_public_tme(url: str):
    """Преобразует ссылку Telegram Web в публичную t.me-ссылку канала.

    https://web.telegram.org/k/#@evacuatejobs -> https://t.me/evacuatejobs
    Для веток публичную ссылку сформировать нельзя -> возвращается None.
    """
    channel, thread = parse_telegram_source(url)
    if not channel or thread:
        return None
    return f"https://t.me/{channel}"


def message_permalink(source_url: str, message_id: str):
    """Строит пермализацию сообщения: https://t.me/<channel>/<messageId>."""
    channel, _ = parse_telegram_source(source_url)
    if not channel or not message_id:
        return ""
    return f"https://t.me/{channel}/{message_id}"


# --------------------------------------------------------------------------- #
# Нормализация URL
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Нормализует URL:
    - приводит scheme/host к нижнему регистру;
    - удаляет tracking-параметры (utm_*, fbclid, gclid, yclid);
    - удаляет завершающий слеш в пути (кроме корня);
    - сохраняет параметр thread в ссылках Telegram Web (он в фрагменте).

    Неизвестные query-параметры сохраняются.
    """
    if not url:
        return ""
    url = str(url).strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    if parts.scheme not in ("http", "https"):
        return url

    # Фильтруем query-параметры (thread здесь не бывает — он в фрагменте).
    kept = []
    for key, val in _iter_query(parts.query):
        if key.lower() in TRACKING_PARAMS:
            continue
        if key.lower().startswith("utm_"):
            continue
        kept.append((key, val))

    from urllib.parse import urlencode, urlunsplit as _urlunsplit

    new_query = urlencode(kept)
    path = parts.path
    # Удаляем завершающий слеш, но не трогаем корень "/".
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
        if not path:
            path = "/"

    return _urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, new_query, parts.fragment)
    )


def _iter_query(query: str):
    if not query:
        return []
    # Ручной разбор, чтобы сохранить порядок и значения (включая пустые).
    out = []
    for pair in query.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
        else:
            k, v = pair, ""
        out.append((k, v))
    return out


def is_direct_link(url: str) -> bool:
    """True, если ссылка ведёт напрямую на вакансию/страницу отклика
    (а не на Telegram)."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in TELEGRAM_HOSTS):
        return False
    return True


# --------------------------------------------------------------------------- #
# Ключ дедупликации
# --------------------------------------------------------------------------- #
def dedup_key(title, company, location, work_mode, url) -> str:
    """Строит ключ дедупликации.

    Полный ключ:  Title + Company + Location + WorkMode + URL
    При пустом URL: Title + Company + Location + WorkMode

    Перед сравнением: удаление лишних пробелов, нижний регистр,
    нормализация дефисов, нормализация WorkMode и URL.
    """
    title = normalize_hyphens(_clean(title)).lower()
    company = normalize_hyphens(_clean(company)).lower()
    location = normalize_hyphens(_clean(location)).lower()
    work_mode = normalize_work_mode(work_mode).lower()
    url_norm = normalize_url(url).lower()

    parts = [title, company, location, work_mode]
    if url_norm:
        parts.append(url_norm)
    return "||".join(parts)


# --------------------------------------------------------------------------- #
# Нормализация вакансии
# --------------------------------------------------------------------------- #
def normalize_job(job: dict) -> dict:
    """Возвращает очищенную копию вакансии с нормализованными полями
    WorkMode и URL. Поля не выдумываются — отсутствующие остаются пустыми."""
    return {
        "Title": _clean(job.get("title") or job.get("Title") or ""),
        "Company": _clean(job.get("company") or job.get("Company") or ""),
        "Location": _clean(job.get("location") or job.get("Location") or ""),
        "WorkMode": normalize_work_mode(job.get("workMode") or job.get("WorkMode") or ""),
        "URL": normalize_url(job.get("url") or job.get("URL") or ""),
    }


def choose_url(source_url: str, message: dict | None, job_url: str) -> str:
    """Выбирает итоговый URL по приоритету:

    1. Прямая ссылка на вакансию/отклик (http(s), не Telegram).
    2. Ссылка на Telegram-публикацию (message.url).
    3. Публичная ссылка на канал (t.me/<channel>).
    4. Исходная ссылка Telegram Web.
    5. Пустое значение.
    """
    if is_direct_link(job_url):
        return normalize_url(job_url)
    if message and message.get("url"):
        return normalize_url(message["url"])
    pub = to_public_tme(source_url)
    if pub:
        return normalize_url(pub)
    if source_url:
        return normalize_url(source_url)
    return ""


# --------------------------------------------------------------------------- #
# Работа с CSV
# --------------------------------------------------------------------------- #
def read_rows(path: str) -> list[dict]:
    """Читает все строки данных (без заголовка) как словари по CSV_HEADER."""
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({k: (row.get(k) or "") for k in CSV_HEADER})
    except FileNotFoundError:
        return []
    return rows


def _existing_keys(rows: list[dict]) -> set[str]:
    keys = set()
    for r in rows:
        keys.add(
            dedup_key(r["Title"], r["Company"], r["Location"], r["WorkMode"], r["URL"])
        )
    return keys


def add_jobs(path: str, jobs: list[dict]):
    """Добавляет новые вакансии в CSV с дедупликацией.

    Возвращает (rows_added, duplicates). Существующие строки и заголовок
    сохраняются. Файл создаётся при первом запуске.
    """
    rows = read_rows(path)
    existing = _existing_keys(rows)
    new_keys = set()

    rows_added = 0
    duplicates = 0

    for job in jobs:
        norm = normalize_job(job)
        key = dedup_key(
            norm["Title"], norm["Company"], norm["Location"], norm["WorkMode"], norm["URL"]
        )
        if not key or key in existing or key in new_keys:
            duplicates += 1
            continue
        new_keys.add(key)
        rows.append(norm)
        rows_added += 1

    _write_rows(path, rows)
    return rows_added, duplicates


def _write_rows(path: str, rows: list[dict]) -> None:
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_HEADER})


def stats_totals(path: str) -> int:
    """Количество строк данных в CSV (без заголовка)."""
    return len(read_rows(path))


def setup_console() -> None:
    """Переключает stdout/stderr в UTF-8, чтобы вывод кириллицы не падал
    на консолях с кодировкой cp1252 (Windows)."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
