"""CSV-хранилище, нормализация и дедупликация для telegram.csv.

Все функции чистые и не зависят от сети, поэтому легко тестируются.
Файл сохраняется в кодировке UTF-8 с корректным экранированием CSV.
"""

from __future__ import annotations

import csv
import os
import re
from urllib.parse import urlsplit

# Порядок колонок CSV (заголовок).
CSV_HEADER = ["Title", "Company", "Location", "WorkMode", "URL"]
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"

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
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


# --------------------------------------------------------------------------- #
# Базовая очистка текста
# --------------------------------------------------------------------------- #
def _clean(value) -> str:
    """Удаляет лишние пробелы по краям и внутри строки."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def get_openrouter_model() -> str:
    """Return the configured OpenRouter model or the project default."""
    return (os.getenv("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL).strip()


def _csv_safe(value) -> str:
    """Neutralize spreadsheet formulas in fields derived from untrusted posts."""
    text = _clean(value)
    if text.startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


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


def strip_work_mode_from_location(location: str, work_mode: str) -> tuple[str, str]:
    """Убирает из location значения, которые являются только режимом работы.

    Если location совпадает с work_mode или целиком является известным
    work-mode токеном, location очищается; work_mode заполняется при необходимости.
    """
    loc = _clean(location)
    wm = normalize_work_mode(work_mode)
    if not loc:
        return "", wm
    loc_key = normalize_hyphens(loc).lower()
    if wm and loc_key == wm.lower():
        return "", wm
    if loc_key in WORK_MODE_MAP:
        return "", wm or WORK_MODE_MAP[loc_key]
    return loc, wm


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


def source_label(url: str) -> str:
    """Короткая метка источника для логов: @channel или @channel?thread=N."""
    channel, thread = parse_telegram_source(url)
    if channel:
        return "@" + channel + (f"?thread={thread}" if thread else "")
    return url or "(unknown)"


import threading

_collect_log_lock = threading.Lock()


def append_collect_log(path: str, message: str) -> None:
    """Дописывает строку в collect.log (единый путь записи для collect и server)."""
    if not path or not message:
        return
    from datetime import datetime

    try:
        ts = datetime.now().strftime("%H:%M:%S")
        with _collect_log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{ts} {message}\n")
    except OSError:
        pass


def to_public_tme(url: str):
    """Преобразует ссылку Telegram Web в публичную t.me-ссылку канала.

    https://web.telegram.org/k/#@evacuatejobs -> https://t.me/evacuatejobs
    Для веток публичную ссылку сформировать нельзя -> возвращается None.
    """
    channel, thread = parse_telegram_source(url)
    if not channel or thread:
        return None
    return f"https://t.me/{channel}"


def normalize_message_id(message_id: str) -> str:
    """Приводит DOM messageId к публичному id для t.me.

    Telegram Web K иногда отдаёт в data-mid значение > 2**32; для ссылок
    нужны младшие 32 бита (например 4295072879 -> 105583).
    """
    s = str(message_id or "").strip()
    if not s:
        return ""
    if not s.isdigit():
        return s
    n = int(s)
    if n > 0xFFFFFFFF:
        n &= 0xFFFFFFFF
    return str(n)


def message_permalink(source_url: str, message_id: str):
    """Строит пермалинк поста.

    Канал: https://t.me/<channel>/<messageId>
    Форум-топик: https://t.me/<channel>/<threadId>/<messageId>
    (см. https://core.telegram.org/api/links#message-links)
    """
    channel, thread = parse_telegram_source(source_url)
    mid = normalize_message_id(message_id)
    if not channel or not mid:
        return ""
    if thread:
        return f"https://t.me/{channel}/{thread}/{mid}"
    return f"https://t.me/{channel}/{mid}"


# --------------------------------------------------------------------------- #
# Нормализация URL
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Нормализует URL:
    - приводит scheme/host к нижнему регистру;
    - удаляет только tracking-параметры (utm_*, fbclid, gclid, yclid);
    - НЕ выполняет повторное percent-encoding query-параметров (сохраняет
      исходные значения как есть);
    - НЕ изменяет значимый путь: `/k/` остаётся `/k/` (не превращается в `/k`);
    - сохраняет fragment целиком, включая параметр `thread`;
    - сохраняет остальные query-параметры и их значения.

    Возвращает исходную строку, если URL не http(s) или неразборный.
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
    if parts.username is not None or parts.password is not None:
        return ""
    if any(char in url for char in ("\r", "\n", "\t")):
        return ""

    # Фильтруем query-параметры (thread здесь не бывает — он в фрагменте).
    kept = []
    for key, val in _iter_query(parts.query):
        kl = key.lower()
        if kl in TRACKING_PARAMS:
            continue
        if kl.startswith("utm_"):
            continue
        kept.append((key, val))

    # Собираем query без повторного percent-encoding (join «key=val» как есть).
    new_query = "&".join(
        (k if v == "" else f"{k}={v}") for k, v in kept
    )

    # Сохраняем исходный путь без изменения значимых сегментов. Удаляем только
    # завершающий слеш на корне «/», но не трогаем сегменты вроде «/k/».
    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
        if path == "":
            path = "/"

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    fragment = parts.fragment
    return f"{scheme}://{netloc}{path}" + (f"?{new_query}" if new_query else "") + (f"#{fragment}" if fragment else "")


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
    if parts.username is not None or parts.password is not None:
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

    Базовый ключ: Title + Company + Location + WorkMode.
    Прямая ссылка на вакансию (не Telegram) добавляется в ключ.
    Telegram-пермалинки и пустой URL в ключ не входят — репосты
  одной вакансии из разных каналов считаются дубликатами.

    Перед сравнением: удаление лишних пробелов, нижний регистр,
    нормализация дефисов, нормализация WorkMode и URL.
    """
    title = normalize_hyphens(_clean(title)).lower()
    company = normalize_hyphens(_clean(company)).lower()
    location = normalize_hyphens(_clean(location)).lower()
    work_mode = normalize_work_mode(work_mode).lower()
    url_norm = normalize_url(url).lower()

    parts = [title, company, location, work_mode]
    if url_norm and is_direct_link(url):
        parts.append(url_norm)
    return "||".join(parts)


# --------------------------------------------------------------------------- #
# Нормализация вакансии
# --------------------------------------------------------------------------- #
def normalize_job(job: dict) -> dict:
    """Возвращает очищенную копию вакансии с нормализованными полями
    WorkMode и URL. Поля не выдумываются — отсутствующие остаются пустыми."""
    loc, wm = strip_work_mode_from_location(
        job.get("location") or job.get("Location") or "",
        job.get("workMode") or job.get("WorkMode") or "",
    )
    return {
        "Title": _csv_safe(job.get("title") or job.get("Title") or ""),
        "Company": _csv_safe(job.get("company") or job.get("Company") or ""),
        "Location": _csv_safe(loc),
        "WorkMode": _csv_safe(wm),
        "URL": normalize_url(job.get("url") or job.get("URL") or ""),
    }


def resolve_job_url(source_url: str, message: dict | None, model_url: str) -> str:
    """Итоговый URL для CSV.

    1. Прямая ссылка на вакансию/отклик из ответа модели (http(s), не Telegram).
    2. Иначе — пермалинк поста из message (url или message_permalink).
    3. Иначе — пустая строка.
    """
    if is_direct_link(model_url):
        return normalize_url(model_url)
    if message:
        url = str(message.get("url") or "").strip()
        if not url:
            url = message_permalink(source_url, str(message.get("messageId") or ""))
        return normalize_url(url) if url else ""
    return ""


# --------------------------------------------------------------------------- #
# Работа с CSV
# --------------------------------------------------------------------------- #
import tempfile

try:
    import fcntl  # available on POSIX; Windows path handled separately below
except ImportError:
    fcntl = None

try:
    import msvcrt
except ImportError:
    msvcrt = None


class _FileLock:
    """Простая блокировка файла, совместимая с Windows и POSIX.

    Защищает read-deduplicate-write блок от одновременных запросов.
    При отсутствии поддержки блокировки — не мешает работе (best effort).
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self):
        lock_path = self.path + ".lock"
        self._fh = open(lock_path, "a+")
        if msvcrt:
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        if self._fh is None:
            return
        try:
            if msvcrt:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


def read_rows(path: str) -> list[dict]:
    """Читает все строки данных (без заголовка) как словари по CSV_HEADER.

    При отсутствии файла возвращает []. При повреждённом/несовместимом
    заголовке выбрасывает ValueError — продолжать нельзя.
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CSV_HEADER:
            raise ValueError(
                f"Несовместимый заголовок CSV: {reader.fieldnames!r}, "
                f"ожидается {CSV_HEADER!r}"
            )
        rows = []
        for row in reader:
            rows.append({k: (row.get(k) or "") for k in CSV_HEADER})
    return rows


def _existing_keys(rows: list[dict]) -> set[str]:
    keys = set()
    for r in rows:
        keys.add(
            dedup_key(r["Title"], r["Company"], r["Location"], r["WorkMode"], r["URL"])
        )
    return keys


def reset_csv(path: str) -> None:
    """Создаёт пустой CSV (только заголовок), перезаписывая существующий файл.

    Вызывается в начале каждого прогона collect.py, чтобы telegram.csv
    содержал только вакансии текущего сбора, а не накапливал данные
    от предыдущих запусков. Внутрипрогонная дедупликация сохраняется
    (одна вакансия из разных источников не дублируется).
    """
    _write_rows(path, [])


def add_jobs_with_report(path: str, jobs: list[dict]):
    """Добавляет вакансии в CSV и возвращает подробный dedup-отчёт.

    Возвращает (rows_added, duplicates, reports), где reports сохраняет порядок
    входных jobs и содержит result: added, duplicate или empty.
    """
    prepared: list[tuple[int, dict, str]] = []
    reports: list[dict] = []
    for idx, job in enumerate(jobs or []):
        norm = normalize_job(job)
        key = dedup_key(
            norm["Title"], norm["Company"], norm["Location"], norm["WorkMode"], norm["URL"]
        )
        if _is_empty_job(norm):
            reports.append({
                "index": idx,
                "result": "empty",
                "dedupKey": key,
                "row": norm,
            })
            continue
        prepared.append((idx, norm, key))

    with _FileLock(path):
        rows = read_rows(path)
        existing = _existing_keys(rows)
        new_keys = set()

        rows_added = 0
        duplicates = 0

        for idx, norm, key in prepared:
            if not key or key in existing or key in new_keys:
                duplicates += 1
                reports.append({
                    "index": idx,
                    "result": "duplicate",
                    "dedupKey": key,
                    "row": norm,
                })
                continue
            new_keys.add(key)
            rows.append(norm)
            rows_added += 1
            reports.append({
                "index": idx,
                "result": "added",
                "dedupKey": key,
                "row": norm,
            })

        _write_rows(path, rows)

    reports.sort(key=lambda item: item["index"])
    return rows_added, duplicates, reports


def add_jobs(path: str, jobs: list[dict]):
    """Добавляет новые вакансии в CSV с дедупликацией.

    Возвращает (rows_added, duplicates). Существующие строки и заголовок
    сохраняются. Файл создаётся при первом запуске. Запись атомарна:
    пишется временный файл в той же директории и заменяется через os.replace.
    Блокировка защищает от одновременных запросов.
    """
    rows_added, duplicates, _reports = add_jobs_with_report(path, jobs)
    return rows_added, duplicates


def _is_empty_job(job: dict) -> bool:
    """True, если вакансия полностью пустая (нет ни одного непустого поля)."""
    return not any(
        (job.get(k) or "").strip()
        for k in ("Title", "Company", "Location", "WorkMode", "URL")
    )


def _write_rows(path: str, rows: list[dict]) -> None:
    """Атомарная запись CSV во временный файл в той же директории + os.replace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp", prefix=".telegram_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER, quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: _csv_safe(r.get(k, "")) for k in CSV_HEADER})
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


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


def setup_file_logging(path: str = "collect.log") -> str:
    """Дублирует stdout/stderr в файл лога через append_collect_log.

    Файл обрезается один раз при старте сессии. Все последующие записи
    (collect и server) идут только через append — без FileHandler, чтобы
    не затирать строки дочернего процесса.
    """
    import sys

    try:
        open(path, "w", encoding="utf-8").close()
    except OSError:
        pass

    class _StdProxy:
        """Прокси для stdout/stderr: пишет в оригинальный поток и в лог-файл."""
        def __init__(self, original):
            self._orig = original
            self._buf = ""

        def write(self, data: str) -> int:
            try:
                self._orig.write(data)
            except Exception:
                pass
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    append_collect_log(path, line.rstrip())
            return len(data)

        def flush(self) -> None:
            try:
                self._orig.flush()
            except Exception:
                pass
            if self._buf.strip():
                append_collect_log(path, self._buf.rstrip())
                self._buf = ""

        def reconfigure(self, **kwargs) -> None:
            if hasattr(self._orig, "reconfigure"):
                try:
                    self._orig.reconfigure(**kwargs)
                except Exception:
                    pass

        @property
        def encoding(self):
            return getattr(self._orig, "encoding", "utf-8")

        def fileno(self):
            return self._orig.fileno()

    sys.stdout = _StdProxy(sys.stdout)
    sys.stderr = _StdProxy(sys.stderr)
    return path
