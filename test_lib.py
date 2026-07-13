"""Тесты ядра: lib.py и server.process_messages (с моком OpenRouter)."""

import importlib.util
import json
import os
import sys
import tempfile
import time as _time
from datetime import datetime as _dt, timezone as _tz, timedelta as _td

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lib
lib.setup_console()


def check(name, got, expected):
    ok = got == expected
    print(("PASS" if ok else "FAIL"), name, "" if ok else f"-> got={got!r} expected={expected!r}")
    if not ok:
        raise SystemExit(1)


# --- normalize_work_mode -------------------------------------------------- #
check("wm remote", lib.normalize_work_mode("remote"), "Remote")
check("wm remotely", lib.normalize_work_mode("Remotely"), "Remote")
check("wm fully remote", lib.normalize_work_mode("FULLY REMOTE"), "Remote")
check("wm 100% remote", lib.normalize_work_mode("100% remote"), "Remote")
check("wm hybrid", lib.normalize_work_mode("Hybrid work"), "Hybrid")
check("wm onsite", lib.normalize_work_mode("on-site"), "On-site")
check("wm office-based", lib.normalize_work_mode("office-based"), "On-site")
check("wm empty", lib.normalize_work_mode(""), "")
check("wm unknown", lib.normalize_work_mode("Sometimes"), "")

# --- parse_telegram_source / to_public_tme -------------------------------- #
check("parse channel", lib.parse_telegram_source("https://web.telegram.org/k/#@evacuatejobs"), ("evacuatejobs", None))
check("parse thread", lib.parse_telegram_source("https://web.telegram.org/k/#@cyprusithr?thread=46685"), ("cyprusithr", "46685"))
check("public tme", lib.to_public_tme("https://web.telegram.org/k/#@evacuatejobs"), "https://t.me/evacuatejobs")
check("public tme thread -> None", lib.to_public_tme("https://web.telegram.org/k/#@cyprusithr?thread=46685"), None)
check("permalink", lib.message_permalink("https://web.telegram.org/k/#@evacuatejobs", "123"), "https://t.me/evacuatejobs/123")

# --- normalize_url -------------------------------------------------------- #
check("url strip utm", lib.normalize_url("https://example.com/job?utm_source=x&id=5"), "https://example.com/job?id=5")
check("url strip trailing slash", lib.normalize_url("https://t.me/evacuatejobs/"), "https://t.me/evacuatejobs")
check("url keep thread (fragment)", lib.normalize_url("https://web.telegram.org/k/#@cyprusithr?thread=46685"), "https://web.telegram.org/k#@cyprusithr?thread=46685")
check("url strip fbclid/gclid", lib.normalize_url("https://x.com/a?fbclid=1&gclid=2&keep=3"), "https://x.com/a?keep=3")
check("url lowercase host", lib.normalize_url("https://EXAMPLE.com/A"), "https://example.com/A")
check("url non-http kept", lib.normalize_url("mailto:a@b.com"), "mailto:a@b.com")

# --- dedup_key ------------------------------------------------------------ #
k1 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw, Poland", "Remote", "https://x.com/1")
k2 = lib.dedup_key(" senior  dev", " hos247", "Warsaw, Poland", "remote", "https://x.com/1/")
check("dedup key normalization", k1, k2)
k3 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw, Poland", "Remote", "")  # empty url -> no url part
check("dedup key no-url drops url", k3, "senior dev||hos247||warsaw, poland||remote")

# --- is_direct_link ------------------------------------------------------- #
check("direct external", lib.is_direct_link("https://linkedin.com/jobs/1"), True)
check("direct t.me not", lib.is_direct_link("https://t.me/evacuatejobs/1"), False)
check("direct web.telegram not", lib.is_direct_link("https://web.telegram.org/k/#@x"), False)

# --- choose_url ----------------------------------------------------------- #
msg = {"url": "https://t.me/evacuatejobs/123"}
check("choose direct", lib.choose_url("https://web.telegram.org/k/#@evacuatejobs", msg, "https://linkedin.com/jobs/1"), "https://linkedin.com/jobs/1")
check("choose message url fallback", lib.choose_url("https://web.telegram.org/k/#@evacuatejobs", msg, ""), "https://t.me/evacuatejobs/123")
check("choose public tme fallback", lib.choose_url("https://web.telegram.org/k/#@evacuatejobs", None, ""), "https://t.me/evacuatejobs")
check("choose source web fallback", lib.choose_url("https://web.telegram.org/k/#@evacuatejobs", None, ""), "https://t.me/evacuatejobs")

# --- add_jobs (CSV roundtrip + dedup) ------------------------------------ #
tmp = tempfile.mkdtemp()
csv_path = os.path.join(tmp, "telegram.csv")
jobs = [
    {"title": "Senior Full-Stack Developer", "company": "HOS247", "location": "Warsaw, Poland", "workMode": "Remote", "url": "https://linkedin.com/jobs/1"},
    {"title": "Senior Full-Stack Developer", "company": "HOS247", "location": "Warsaw, Poland", "workMode": "Remote", "url": "https://linkedin.com/jobs/1/"},  # dup (trailing slash)
    {"title": "Frontend Engineer", "company": "Acme", "location": "Berlin, Germany", "workMode": "Hybrid", "url": "https://jobs.acme.com/fe"},
]
added, dups = lib.add_jobs(csv_path, jobs)
check("add_jobs added", added, 2)
check("add_jobs dups", dups, 1)

# re-add same + one new
jobs2 = [
    {"title": "Senior Full-Stack Developer", "company": "HOS247", "location": "Warsaw, Poland", "workMode": "Remote", "url": "https://linkedin.com/jobs/1"},
    {"title": "Backend Engineer", "company": "NewCo", "location": "Remote", "workMode": "Remote", "url": "https://newco.com/b"},
]
added2, dups2 = lib.add_jobs(csv_path, jobs2)
check("add_jobs2 added", added2, 1)
check("add_jobs2 dups", dups2, 1)

# verify CSV content + quoting
with open(csv_path, encoding="utf-8") as f:
    content = f.read()
print("--- CSV ---")
print(content)
check("csv header", content.splitlines()[0], "Title,Company,Location,WorkMode,URL")
check("csv rows count", len(content.strip().splitlines()), 4)  # header + 3 unique

# --- reset_csv ------------------------------------------------------------ #
# reset_csv должен перезаписывать файл: только заголовок, без строк данных.
csv_reset = os.path.join(tmp, "reset_test.csv")
lib.add_jobs(csv_reset, jobs)  # пишем 2 уникальные вакансии
check("reset: rows before reset", len(lib.read_rows(csv_reset)), 2)
lib.reset_csv(csv_reset)
rows_after = lib.read_rows(csv_reset)
check("reset: rows after reset", len(rows_after), 0)
with open(csv_reset, encoding="utf-8") as f:
    header = f.readline().strip()
check("reset: header preserved", header, "Title,Company,Location,WorkMode,URL")
# После reset можно снова добавлять вакансии (дедупликация с пустым файлом).
added3, dups3 = lib.add_jobs(csv_reset, jobs)
check("reset: add after reset works", added3, 2)

print("\n[lib] все проверки пройдены")


# --- _parse_tabs (browser_agent) ----------------------------------------- #
import browser_agent as ba

# JSON-формат
json_result = '{"tabs": [{"tabId": "1", "url": "chrome-extension://abc/connect.html", "title": "Connect"}, {"tabId": "2", "url": "https://web.telegram.org/k/", "title": "Telegram"}]}'
# browser_tabs может вернуть просто массив
json_arr = '[{"tabId": "1", "url": "chrome-extension://abc/connect.html"}, {"tabId": "2", "url": "https://example.com"}]'
tabs1 = ba._parse_tabs(json_arr)
check("parse_tabs json count", len(tabs1), 2)
check("parse_tabs json tabId", tabs1[0]["tabId"], "1")
check("parse_tabs json url", "connect.html" in tabs1[0]["url"], True)
check("parse_tabs json tab2", tabs1[1]["tabId"], "2")

# MCP-формат с ### Result (текстовый: "- 0: (current) [Title](url)")
mcp_text = '### Result\n- 0: (current) [Welcome](chrome-extension://abc/connect.html?token=xyz)\n- 1: [Telegram](https://web.telegram.org/k/)\n### Ran Playwright code\n...'
tabs2 = ba._parse_tabs(mcp_text)
check("parse_tabs mcp count", len(tabs2), 2)
check("parse_tabs mcp tabId", tabs2[0]["tabId"], "0")
check("parse_tabs mcp connect url", "connect.html" in tabs2[0]["url"], True)
check("parse_tabs mcp tab2 id", tabs2[1]["tabId"], "1")
check("parse_tabs mcp tab2 url", "web.telegram.org" in tabs2[1]["url"], True)

# Пустой результат
check("parse_tabs empty", ba._parse_tabs(""), [])
check("parse_tabs none", ba._parse_tabs("no tabs here"), [])

print("\n[browser_agent._parse_tabs] все проверки пройдены")


# --- server.process_messages с моком OpenRouter -------------------------- #
import server as server_mod

# Мок ответа модели: одно сообщение с двумя подходящими вакансиями.
FAKE_MODEL_RESPONSE = json.dumps({
    "jobs": [
        {"title": "Senior Full-Stack Developer", "company": "HOS247",
         "location": "Warsaw, Mazowieckie, Poland", "workMode": "fully remote",
         "url": "https://www.linkedin.com/jobs/view/4410073565/"},
        {"title": "React Developer", "company": "HOS247",
         "location": "Warsaw, Poland", "workMode": "remote",
         "url": "https://t.me/evacuatejobs/9"},  # t.me -> должно остаться как priority 2
    ]
})

def fake_call(model, user_prompt):
    return FAKE_MODEL_RESPONSE

server_mod._call_openrouter = fake_call

csv2 = os.path.join(tmp, "telegram2.csv")
orig = server_mod.CSV_PATH
server_mod.CSV_PATH = csv2
try:
    fresh_at = (_dt.now(_tz.utc) - _td(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "9", "text": "We are hiring...", "publishedAt": fresh_at,
             "url": "https://t.me/evacuatejobs/9", "links": ["https://www.linkedin.com/jobs/view/4410073565/"]},
        ],
    )
finally:
    server_mod.CSV_PATH = orig

check("process messagesReceived", stats["messagesReceived"], 1)
check("process jobsExtracted", stats["jobsExtracted"], 2)
check("process rowsAdded", stats["rowsAdded"], 2)
check("process duplicates", stats["duplicates"], 0)
check("process errors empty", stats["errors"], [])

with open(csv2, encoding="utf-8") as f:
    print("--- telegram2.csv ---")
    print(f.read())

print("\n[server] process_messages OK")


# --- server._cleanup_lock (atexit) --------------------------------------- #
import atexit as _atexit

# 1) Функция очистки зарегистрирована в atexit (импорт server.py регистрирует её).
n_before = _atexit._ncallbacks() if hasattr(_atexit, "_ncallbacks") else 0
check("cleanup registered", n_before > 0 and callable(server_mod._cleanup_lock), True)

csv3 = os.path.join(tmp, "telegram3.csv")
lock3 = csv3 + ".lock"
orig = server_mod.CSV_PATH
server_mod.CSV_PATH = csv3
try:
    # 2) add_jobs создаёт .lock-файл (побочный эффект _FileLock).
    lib.reset_csv(csv3)
    lib.add_jobs(csv3, [{"title": "Dev", "company": "Co", "location": "",
                          "workMode": "Remote", "url": "https://x.test/1"}])
    check("lock created by add_jobs", os.path.exists(lock3), True)

    # 3) CSV существует и содержит данные.
    check("csv exists before cleanup", os.path.exists(csv3), True)

    # 4) _cleanup_lock удаляет только .lock, не трогая CSV.
    server_mod._cleanup_lock()
    check("lock removed by cleanup", os.path.exists(lock3), False)
    check("csv preserved by cleanup", os.path.exists(csv3), True)

    # 5) _cleanup_lock безопасна при отсутствии .lock (не падает).
    server_mod._cleanup_lock()
    check("cleanup no-op when no lock", os.path.exists(lock3), False)
finally:
    server_mod.CSV_PATH = orig
    for p in (lock3, csv3):
        if os.path.exists(p):
            os.remove(p)

print("\n[server._cleanup_lock] все проверки пройдены")


# --- R4: фильтр по давности сообщений (TELEGRAM_SINCE_HOURS) --------------- #
from server import MessageIn as _Msg

def _iso(delta_hours: float) -> str:
    """ISO 8601 (UTC) метка, отстоящая от now на delta_hours."""
    t = _dt.now(_tz.utc) + _td(hours=delta_hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

# _parse_published_at
check("parse Z", server_mod._parse_published_at("2026-07-13T10:00:00Z") is not None, True)
check("parse empty", server_mod._parse_published_at(""), None)
check("parse None-ish", server_mod._parse_published_at("not-a-date"), None)
check("parse tz offset", server_mod._parse_published_at("2026-07-13T10:00:00+00:00") is not None, True)

# filter_by_since — базовые сценарии
fresh = _Msg(text="fresh", publishedAt=_iso(-1))     # 1ч назад — свежее
stale = _Msg(text="stale", publishedAt=_iso(-48))    # 48ч назад — старое
no_ts = _Msg(text="no-ts", publishedAt="")            # без метки

kept, dropped = server_mod.filter_by_since([fresh, stale, no_ts], 24)
check("filter keeps fresh", len(kept), 2)             # fresh + no_ts
check("filter drops stale", dropped, 1)
check("filter keeps no-ts in kept", any(m.text == "no-ts" for m in kept), True)
check("filter drops stale not in kept", any(m.text == "stale" for m in kept), False)

# все старые — все отброшены
kept_all_old, dropped_all_old = server_mod.filter_by_since([stale, stale], 24)
check("filter all old kept", len(kept_all_old), 0)
check("filter all old dropped", dropped_all_old, 2)

# интеграционный тест: process_messages возвращает filteredByTime
csv4 = os.path.join(tmp, "telegram4.csv")
orig_csv = server_mod.CSV_PATH
orig_since = server_mod.SINCE_HOURS
server_mod.CSV_PATH = csv4
server_mod.SINCE_HOURS = 24
try:
    lib.reset_csv(csv4)
    stats4 = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "fresh job", "publishedAt": _iso(-1),
             "url": "", "links": []},
            {"messageId": "2", "text": "old job", "publishedAt": _iso(-100),
             "url": "", "links": []},
        ],
    )
    check("process filteredByTime", stats4["filteredByTime"], 1)
    check("process messagesReceived", stats4["messagesReceived"], 2)
    check("process not skipped", stats4.get("skipped", ""), "")
    check("process no errors", stats4["errors"], [])
finally:
    server_mod.CSV_PATH = orig_csv
    server_mod.SINCE_HOURS = orig_since
    if os.path.exists(csv4):
        os.remove(csv4)
    if os.path.exists(csv4 + ".lock"):
        os.remove(csv4 + ".lock")

# Интеграционный тест: все сообщения старые -> skipped, не errors
csv5 = os.path.join(tmp, "telegram5.csv")
server_mod.CSV_PATH = csv5
try:
    lib.reset_csv(csv5)
    stats5 = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "old job 1", "publishedAt": _iso(-100),
             "url": "", "links": []},
            {"messageId": "2", "text": "old job 2", "publishedAt": _iso(-200),
             "url": "", "links": []},
        ],
    )
    check("process all-old skipped", bool(stats5.get("skipped", "")), True)
    check("process all-old no errors", stats5["errors"], [])
    check("process all-old filteredByTime", stats5["filteredByTime"], 2)
finally:
    server_mod.CSV_PATH = orig_csv
    server_mod.SINCE_HOURS = orig_since
    if os.path.exists(csv5):
        os.remove(csv5)
    if os.path.exists(csv5 + ".lock"):
        os.remove(csv5 + ".lock")

print("\n[server.filter_by_since] все проверки пройдены")


# --- R4: scroll-collect helpers (browser_agent) -------------------------- #
import asyncio as _asyncio
import browser_agent as ba

fresh_iso = _iso(-1)
old_iso = _iso(-48)

# _parse_iso_ts
check("iso parse Z", ba._parse_iso_ts("2026-07-13T10:00:00Z") is not None, True)
check("iso parse empty", ba._parse_iso_ts(""), None)
check("iso parse invalid", ba._parse_iso_ts("not-a-date"), None)
check("iso parse offset", ba._parse_iso_ts("2026-07-13T12:00:00+02:00") is not None, True)

# _detect_scroll_direction
old_first = [{"publishedAt": old_iso}, {"publishedAt": fresh_iso}]
check("dir old-first -> top is older", ba._detect_scroll_direction(old_first), ("top", "bottom"))

new_first = [{"publishedAt": fresh_iso}, {"publishedAt": old_iso}]
check("dir new-first -> bottom is older", ba._detect_scroll_direction(new_first), ("bottom", "top"))

check("dir single -> default", ba._detect_scroll_direction([{"publishedAt": fresh_iso}]), ("top", "bottom"))
check("dir no-ts -> default", ba._detect_scroll_direction([{"publishedAt": ""}, {"publishedAt": ""}]), ("top", "bottom"))

# _merge_by_id
acc: dict = {}
n = ba._merge_by_id(acc, [{"messageId": "a", "text": "x"}, {"messageId": "b", "text": "y"}])
check("merge new count", n, 2)
check("merge size", len(acc), 2)
n2 = ba._merge_by_id(acc, [{"messageId": "a", "text": "x"}, {"messageId": "c", "text": "z"}])
check("merge dedup count", n2, 1)
check("merge size after dedup", len(acc), 3)

# _scroll_js
check("scroll top js has scrollTop=0", "scrollTop = 0" in ba._scroll_js("top"), True)
check("scroll bottom js has scrollHeight", "scrollHeight" in ba._scroll_js("bottom"), True)

print("\n[browser_agent scroll helpers] все проверки пройдены")


# --- R4: collect_with_scroll — mocked MCP loop --------------------------- #
def _msg_json(msgs):
    return json.dumps(msgs)


class _FakeMCP:
    """Мок MCPSession для collect_with_scroll.

    extract_responses — список JSON-строк, возвращаемых по очереди для
    каждого вызова browser_evaluate с EXTRACT_MESSAGES_JS.
    Вызовы со scrollTop (прокрутка) — no-op, считаются в scroll_count.
    """
    def __init__(self, extract_responses):
        self._r = list(extract_responses)
        self._i = 0
        self.scroll_count = 0

    async def call(self, tool, args, timeout=None):
        if tool != "browser_evaluate":
            return ""
        fn = args.get("function", "")
        if "scrollTop" in fn:
            self.scroll_count += 1
            return "ok"
        r = self._r[min(self._i, len(self._r) - 1)]
        self._i += 1
        return r


# Сценарий 1: приземлились на старых сообщениях, скроллим к новым, потом к старым.
#   extract 0 (initial): 2 old messages
#   phase 1 scroll newer -> extract 1: 2 fresh messages
#   phase 1 scroll newer -> extract 2: same fresh (no new -> stop)
#   phase 2 init -> extract 3: same fresh
#   phase 2 scroll older -> extract 4: same fresh (no new -> stop)
extracts = [
    _msg_json([
        {"messageId": "a", "text": "old1", "publishedAt": old_iso, "url": "", "links": []},
        {"messageId": "b", "text": "old2", "publishedAt": old_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
]
fake2 = _FakeMCP(extracts)
res2 = _asyncio.run(ba.collect_with_scroll(fake2, "https://web.telegram.org/k/#@t", 24))
check("scroll merged 4 unique", len(res2), 4)
check("scroll sorted old first", res2[0]["messageId"], "a")
check("scroll sorted recent last", res2[-1]["messageId"], "d")
check("scroll phase1+phase2 did scroll", fake2.scroll_count > 0, True)

# Сценарий 3: приземлились на свежих сообщениях (хвост), старых нет.
#   extract 0 (initial): 2 fresh
#   phase 1: newest_ts fresh -> break immediately
#   phase 2 init -> extract 1: same fresh
#   phase 2: oldest_ts fresh (not < cutoff), scroll older
#   phase 2 -> extract 2: same fresh (no new -> stop)
extracts3 = [
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
]
fake3 = _FakeMCP(extracts3)
res3 = _asyncio.run(ba.collect_with_scroll(fake3, "https://web.telegram.org/k/#@t", 24))
check("scroll at tail returns 2", len(res3), 2)
check("scroll at tail sorted", res3[0]["messageId"], "c")

# Сценарий 4: пустой канал (нет сообщений).
fake4 = _FakeMCP([_msg_json([])])
res4 = _asyncio.run(ba.collect_with_scroll(fake4, "https://web.telegram.org/k/#@t", 24))
check("scroll empty returns []", res4, [])

# Сценарий 5: сообщения без меток времени, каждая прокрутка загружает новые.
# Раньше цикл уходил в бесконечную прокрутку (15 итераций в каждую фазу).
# Теперь: фаза 1 и фаза 2 останавливаются после 3 итераций без timestamps.
extracts5 = [_msg_json([
    {"messageId": f"m{i}", "text": f"msg{i}", "publishedAt": "", "url": "", "links": []}
]) for i in range(40)]
fake5 = _FakeMCP(extracts5)
res5 = _asyncio.run(ba.collect_with_scroll(fake5, "https://web.telegram.org/k/#@t", 24, max_iter=15))
# 1 initial + 3 phase1 + 1 phase2-init + 3 phase2 = max 8 messages, 6 scrolls
check("scroll no-ts did not exhaust max_iter", fake5.scroll_count < 10, True)
check("scroll no-ts bounded message count", len(res5) <= 10, True)

# Сценарий 6: timestamps отсутствуют в первом extract, heal_callback
# возвращает новый JS, второй extract (с healed JS) возвращает timestamps.
# _FakeMCP возвращает разные результаты в зависимости от того, какой JS
# выполняется: если содержит "healed" — с timestamps, иначе — без.
class _FakeMCPHeal:
    def __init__(self):
        self.scroll_count = 0
        self.extract_count = 0

    async def call(self, tool, args, timeout=None):
        if tool != "browser_evaluate":
            return ""
        fn = args.get("function", "")
        if "scrollTop" in fn:
            self.scroll_count += 1
            return "ok"
        self.extract_count += 1
        if "healed" in fn:
            return _msg_json([
                {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
                {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
            ])
        return _msg_json([
            {"messageId": "c", "text": "new1", "publishedAt": "", "url": "", "links": []},
            {"messageId": "d", "text": "new2", "publishedAt": "", "url": "", "links": []},
        ])


async def _heal_cb_ok(mcp):
    return "() => { return JSON.stringify([{messageId:'c',text:'new1',publishedAt:'" + fresh_iso + "',url:'',links:[]}]); } // healed"


fake6 = _FakeMCPHeal()
res6 = _asyncio.run(ba.collect_with_scroll(
    fake6, "https://web.telegram.org/k/#@t", 24, heal_callback=_heal_cb_ok
))
check("heal triggered: messages have timestamps", ba._has_timestamps(res6), True)
check("heal triggered: returns 2 messages", len(res6), 2)
check("heal triggered: extract called >= 2 (initial + post-heal)", fake6.extract_count >= 2, True)

# Сценарий 7: heal_callback возвращает "" (AI не смог) — bounded scroll.
class _FakeMCPHealFail:
    def __init__(self):
        self.scroll_count = 0
        self.extract_count = 0

    async def call(self, tool, args, timeout=None):
        if tool != "browser_evaluate":
            return ""
        fn = args.get("function", "")
        if "scrollTop" in fn:
            self.scroll_count += 1
            return "ok"
        self.extract_count += 1
        return _msg_json([
            {"messageId": f"m{self.extract_count}", "text": "x", "publishedAt": "", "url": "", "links": []},
        ])


async def _heal_cb_fail(mcp):
    return ""


fake7 = _FakeMCPHealFail()
res7 = _asyncio.run(ba.collect_with_scroll(
    fake7, "https://web.telegram.org/k/#@t", 24, heal_callback=_heal_cb_fail
))
check("heal failed: bounded scroll", fake7.scroll_count < 10, True)
check("heal failed: still returns messages", len(res7) > 0, True)

print("\n[browser_agent.collect_with_scroll heal] все проверки пройдены")
