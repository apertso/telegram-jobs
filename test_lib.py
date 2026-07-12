"""Тесты ядра: lib.py и server.process_messages (с моком OpenRouter)."""

import importlib.util
import json
import os
import sys
import tempfile

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
    stats = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "9", "text": "We are hiring...", "publishedAt": "2026-07-11T10:00:00Z",
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
