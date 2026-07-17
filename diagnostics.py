"""Structured JSONL diagnostics for the Telegram jobs pipeline.

Diagnostics are intentionally off by default. The module has no third-party
dependencies and never raises into the main collection flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FILES = {
    "messages": "messages.jsonl",
    "ai_requests": "ai_requests.jsonl",
    "ai_responses": "ai_responses.jsonl",
    "jobs": "jobs.jsonl",
}

DIAGNOSTICS_DIR = Path("diagnostics")
_LOCK = threading.Lock()
_RUN_ID: str | None = None
_REDACTED = "[REDACTED]"
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|auth[_-]?token|access[_-]?token|token|secret|password|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9._-]+"),
    re.compile(r"sk-[A-Za-z0-9._-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"([?&](?:token|access_token|auth_token|api_key|apikey|secret)=)[^&\s]+", re.IGNORECASE),
]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on", "y"}


def enabled() -> bool:
    return _truthy(os.getenv("AI_DIAGNOSTICS_ENABLED"))


def diagnostics_dir() -> Path:
    return DIAGNOSTICS_DIR.resolve()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_run_id() -> str:
    global _RUN_ID
    env_run_id = (os.getenv("AI_DIAGNOSTICS_RUN_ID") or "").strip()
    if env_run_id:
        _RUN_ID = env_run_id
        return env_run_id
    if _RUN_ID is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _RUN_ID = f"{stamp}-{os.getpid()}"
        os.environ["AI_DIAGNOSTICS_RUN_ID"] = _RUN_ID
    return _RUN_ID


def clean_start() -> None:
    root = diagnostics_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for path in root.glob("*.jsonl"):
            path.unlink()
    except OSError:
        return


def short_hash(value: Any, length: int = 12) -> str:
    data = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()[:length]


def message_ref(source: str, message: Any) -> str:
    if isinstance(message, dict):
        mid = str(message.get("messageId") or "").strip()
        text = str(message.get("text") or "")
        url = str(message.get("url") or "")
        published_at = str(message.get("publishedAt") or "")
    else:
        mid = str(getattr(message, "messageId", "") or "").strip()
        text = str(getattr(message, "text", "") or "")
        url = str(getattr(message, "url", "") or "")
        published_at = str(getattr(message, "publishedAt", "") or "")
    suffix = mid or short_hash(f"{text}|{url}|{published_at}")
    return f"{short_hash(source)}:{suffix}"


def ai_request_id(source: str, purpose: str, part: int | str, attempt: int | None = None) -> str:
    raw = f"{ensure_run_id()}|{source}|{purpose}|{part}|{attempt or ''}"
    return f"ai_{short_hash(raw, 16)}"


def candidate_id(ai_request_id_value: str, index: int) -> str:
    return f"{ai_request_id_value}:job:{index + 1}"


def _env_secret_values() -> list[str]:
    out: list[str] = []
    for key, value in os.environ.items():
        if value and len(value) >= 8 and _SECRET_KEY_RE.search(key):
            out.append(value)
    return out


def _sanitize_string(value: str) -> str:
    text = value
    for secret in _env_secret_values():
        text = text.replace(secret, _REDACTED)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + _REDACTED) if m.lastindex else _REDACTED, text)
    return text


def sanitize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_s = str(key)
            out[key_s] = _REDACTED if _SECRET_KEY_RE.search(key_s) else sanitize(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_string(str(value))


def write_log(kind: str, record: dict[str, Any]) -> None:
    if not enabled():
        return
    filename = FILES.get(kind)
    if not filename:
        return
    try:
        root = diagnostics_dir()
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": now_iso(),
            "runId": ensure_run_id(),
            **record,
        }
        line = json.dumps(sanitize(payload), ensure_ascii=False, sort_keys=True)
        with _LOCK:
            with (root / filename).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
