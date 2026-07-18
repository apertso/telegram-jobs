"""Тесты ядра: lib.py и server.process_messages (с моком OpenRouter)."""

import importlib.util
import contextlib
import io
import inspect
import json
import os
import re
import sys
import tempfile
import time as _time
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lib
import diagnostics
lib.setup_console()


def check(name, got, expected):
    ok = got == expected
    print(("PASS" if ok else "FAIL"), name, "" if ok else f"-> got={got!r} expected={expected!r}")
    if not ok:
        raise SystemExit(1)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


_original_model_env = os.environ.get("OPENROUTER_MODEL")
try:
    os.environ["OPENROUTER_MODEL"] = ""
    check(
        "default OpenRouter model",
        lib.get_openrouter_model(),
        "deepseek/deepseek-v4-flash",
    )
    os.environ["OPENROUTER_MODEL"] = "custom/model"
    check("OpenRouter model override", lib.get_openrouter_model(), "custom/model")
finally:
    if _original_model_env is None:
        os.environ.pop("OPENROUTER_MODEL", None)
    else:
        os.environ["OPENROUTER_MODEL"] = _original_model_env


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

# --- strip_work_mode_from_location / normalize_job ------------------------ #
check("strip remote dup", lib.strip_work_mode_from_location("Remote", "Remote"), ("", "Remote"))
check("strip warsaw unchanged", lib.strip_work_mode_from_location("Warsaw, Poland", "Remote"), ("Warsaw, Poland", "Remote"))
check("strip infer wm", lib.strip_work_mode_from_location("fully remote", ""), ("", "Remote"))
nj = lib.normalize_job({"title": "Dev", "company": "Eshe", "location": "Remote", "workMode": "Remote"})
check("normalize_job strips remote loc", nj["Location"], "")
check("normalize_job keeps wm", nj["WorkMode"], "Remote")

# --- parse_telegram_source / to_public_tme -------------------------------- #
check("parse channel", lib.parse_telegram_source("https://web.telegram.org/k/#@evacuatejobs"), ("evacuatejobs", None))
check("parse thread", lib.parse_telegram_source("https://web.telegram.org/k/#@cyprusithr?thread=46685"), ("cyprusithr", "46685"))
check("source label channel", lib.source_label("https://web.telegram.org/k/#@evacuatejobs"), "@evacuatejobs")
check("source label thread", lib.source_label("https://web.telegram.org/k/#@cyprusithr?thread=46685"), "@cyprusithr?thread=46685")
check("public tme", lib.to_public_tme("https://web.telegram.org/k/#@evacuatejobs"), "https://t.me/evacuatejobs")
check("public tme thread -> None", lib.to_public_tme("https://web.telegram.org/k/#@cyprusithr?thread=46685"), None)
check("permalink", lib.message_permalink("https://web.telegram.org/k/#@evacuatejobs", "123"), "https://t.me/evacuatejobs/123")
check("normalize message id packed", lib.normalize_message_id("4295072879"), "105583")
check("normalize message id small", lib.normalize_message_id("9"), "9")
check("permalink with thread", lib.message_permalink("https://web.telegram.org/k/#@cyprusithr?thread=46685", "4295072879"), "https://t.me/cyprusithr/46685/105583")

# --- normalize_url -------------------------------------------------------- #
check("url strip utm", lib.normalize_url("https://example.com/job?utm_source=x&id=5"), "https://example.com/job?id=5")
check("url strip trailing slash", lib.normalize_url("https://t.me/evacuatejobs/"), "https://t.me/evacuatejobs")
check("url keep thread (fragment)", lib.normalize_url("https://web.telegram.org/k/#@cyprusithr?thread=46685"), "https://web.telegram.org/k#@cyprusithr?thread=46685")
check("url strip fbclid/gclid", lib.normalize_url("https://x.com/a?fbclid=1&gclid=2&keep=3"), "https://x.com/a?keep=3")
check("url lowercase host", lib.normalize_url("https://EXAMPLE.com/A"), "https://example.com/A")
check(
    "url non-http kept",
    lib.normalize_url("mailto:user" + "@example.test"),
    "mailto:user" + "@example.test",
)
credential_url = "https://user" + ":pass" + "@example.test/job"
check("url credentials rejected", lib.normalize_url(credential_url), "")

# --- dedup_key ------------------------------------------------------------ #
k1 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw, Poland", "Remote", "https://x.com/1")
k2 = lib.dedup_key(" senior  dev", " hos247", "Warsaw, Poland", "remote", "https://x.com/1/")
check("dedup key normalization", k1, k2)
k3 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw, Poland", "Remote", "")  # empty url -> no url part
check("dedup key no-url drops url", k3, "senior dev||hos247||warsaw, poland||remote")
k4 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw", "Remote", "https://t.me/channel_a/100")
k5 = lib.dedup_key("Senior Dev", "HOS247", "Warsaw", "Remote", "https://t.me/channel_b/200")
check("dedup key t.me reposts same key", k4, k5)

# --- is_direct_link ------------------------------------------------------- #
check("direct external", lib.is_direct_link("https://linkedin.com/jobs/1"), True)
check("direct t.me not", lib.is_direct_link("https://t.me/evacuatejobs/1"), False)
check("direct web.telegram not", lib.is_direct_link("https://web.telegram.org/k/#@x"), False)

# --- resolve_job_url ------------------------------------------------------ #
src = "https://web.telegram.org/k/#@evacuatejobs"
msg = {"messageId": "123", "url": "https://t.me/evacuatejobs/123"}
check("resolve direct", lib.resolve_job_url(src, msg, "https://linkedin.com/jobs/1"), "https://linkedin.com/jobs/1")
check("resolve permalink when empty", lib.resolve_job_url(src, msg, ""), "https://t.me/evacuatejobs/123")
check("resolve permalink ignores t.me from model", lib.resolve_job_url(src, msg, "https://t.me/other/1"), "https://t.me/evacuatejobs/123")
check("resolve builds permalink from messageId", lib.resolve_job_url(src, {"messageId": "456", "url": ""}, ""), "https://t.me/evacuatejobs/456")
check("resolve permalink with thread", lib.resolve_job_url(
    "https://web.telegram.org/k/#@cyprusithr?thread=46685",
    {"messageId": "105583", "url": ""},
    "",
), "https://t.me/cyprusithr/46685/105583")
check("resolve permalink packed messageId", lib.resolve_job_url(
    "https://web.telegram.org/k/#@cyprusithr?thread=46685",
    {"messageId": "4295072879", "url": ""},
    "",
), "https://t.me/cyprusithr/46685/105583")
check("resolve no message no url", lib.resolve_job_url(src, None, ""), "")

# --- add_jobs (CSV roundtrip + dedup) ------------------------------------ #
tmp = tempfile.mkdtemp()
csv_path = os.path.join(tmp, "telegram.csv")

# t.me repost dedup: одинаковые вакансии из разных каналов — один ключ.
csv_repost = os.path.join(tmp, "repost.csv")
lib.reset_csv(csv_repost)
repost_jobs = [
    {"title": "Senior Dev", "company": "HOS247", "location": "Warsaw", "workMode": "Remote",
     "url": "https://t.me/channel_a/100"},
    {"title": "Senior Dev", "company": "HOS247", "location": "Warsaw", "workMode": "Remote",
     "url": "https://t.me/channel_b/200"},
]
added_r, dups_r = lib.add_jobs(csv_repost, repost_jobs)
check("t.me repost added", added_r, 1)
check("t.me repost dups", dups_r, 1)
direct_jobs = [
    {"title": "Senior Dev", "company": "HOS247", "location": "Warsaw", "workMode": "Remote",
     "url": "https://linkedin.com/jobs/1"},
    {"title": "Senior Dev", "company": "HOS247", "location": "Warsaw", "workMode": "Remote",
     "url": "https://linkedin.com/jobs/2"},
]
added_d, dups_d = lib.add_jobs(csv_repost, direct_jobs)
check("different direct urls added", added_d, 2)
check("different direct urls no dups", dups_d, 0)

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

csv_report = os.path.join(tmp, "report.csv")
lib.reset_csv(csv_report)
added_report, dups_report, reports = lib.add_jobs_with_report(csv_report, [
    {"title": "Senior Dev", "company": "Acme", "location": "Berlin", "workMode": "Remote",
     "url": "https://jobs.example.com/1"},
    {"title": "Senior Dev", "company": "Acme", "location": "Berlin", "workMode": "Remote",
     "url": "https://jobs.example.com/1/"},
    {"title": "", "company": "", "location": "", "workMode": "", "url": ""},
])
check("add_jobs report added", added_report, 1)
check("add_jobs report dups", dups_report, 1)
check("add_jobs report results", [r["result"] for r in reports], ["added", "duplicate", "empty"])
check("add_jobs report dedup key", bool(reports[0]["dedupKey"]), True)

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

csv_formula = os.path.join(tmp, "formula.csv")
lib.reset_csv(csv_formula)
lib.add_jobs(csv_formula, [{
    "title": "=HYPERLINK(\"https://example.test\",\"click\")",
    "company": "+cmd|' /C calc'!A0",
    "location": "@SUM(1+1)",
    "workMode": "Remote",
    "url": "https://example.test/job",
}])
formula_row = lib.read_rows(csv_formula)[0]
check("csv formula title escaped", formula_row["Title"].startswith("'="), True)
check("csv formula company escaped", formula_row["Company"].startswith("'+"), True)
check("csv formula location escaped", formula_row["Location"].startswith("'@"), True)

print("\n[lib] все проверки пройдены")


# --- _parse_tabs (browser_agent) ----------------------------------------- #
import browser_agent as ba

_mcp_env_keys = ["OPENROUTER_API_KEY", "UNRELATED_PASSWORD", "PLAYWRIGHT_MCP_EXTENSION_TOKEN"]
_mcp_env_orig = {key: os.environ.get(key) for key in _mcp_env_keys}
try:
    os.environ["OPENROUTER_API_KEY"] = "test-openrouter-secret"
    os.environ["UNRELATED_PASSWORD"] = "test-password-secret"
    os.environ["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = "test-extension-token"
    child_env = ba._mcp_subprocess_env()
    check("mcp env excludes OpenRouter key", "OPENROUTER_API_KEY" in child_env, False)
    check("mcp env excludes unrelated secret", "UNRELATED_PASSWORD" in child_env, False)
    check("mcp env keeps extension token", child_env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"], "test-extension-token")
finally:
    for key, value in _mcp_env_orig.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

check(
    "mcp allowlist is minimal",
    ba.ALLOWED_MCP_TOOLS,
    {"browser_tabs", "browser_navigate", "browser_snapshot", "browser_evaluate"},
)
check("locked MCP CLI is installed", ba.MCP_CLI.is_file(), True)
check(
    "scroll collector accepts no generated JS",
    {"extract_js", "heal_callback"}.isdisjoint(inspect.signature(ba.collect_with_scroll).parameters),
    True,
)
check(
    "extractor selects Telegram message bubbles",
    "querySelectorAll('div.bubble[data-mid]')" in ba.EXTRACT_MESSAGES_JS,
    True,
)
check(
    "extractor skips non-positive service ids",
    "Number(mid) <= 0" in ba.EXTRACT_MESSAGES_JS,
    True,
)

_duplicate_dom_messages = ba._clean_messages(
    {
        "messages": [
            {"messageId": "42", "text": "same post", "publishedAt": "", "url": "", "links": []},
            {
                "messageId": "42",
                "text": "same post",
                "publishedAt": "2026-07-18T10:00:00Z",
                "url": "",
                "links": [],
            },
        ]
    },
    "https://web.telegram.org/k/#@test",
)
check("clean duplicate DOM message count", len(_duplicate_dom_messages), 1)
check(
    "clean duplicate DOM message fills timestamp",
    _duplicate_dom_messages[0]["publishedAt"],
    "2026-07-18T10:00:00Z",
)
check(
    "clean messages skips negative service id",
    ba._clean_messages(
        {"messages": [{"messageId": "-1", "text": "service", "publishedAt": "1970-01-01T00:00:00Z"}]},
        "https://web.telegram.org/k/#@test",
    ),
    [],
)


class _SnapshotMCP:
    async def call(self, name, arguments, timeout=30):
        return "private-message-that-must-not-be-logged"


_snapshot_stderr = io.StringIO()
with contextlib.redirect_stderr(_snapshot_stderr):
    _snapshot_active = __import__("asyncio").run(
        ba.telegram_session_active(_SnapshotMCP())
    )
check("unknown snapshot is inactive", _snapshot_active, False)
check(
    "unknown snapshot content is not logged",
    "private-message-that-must-not-be-logged" in _snapshot_stderr.getvalue(),
    False,
)

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
check("parse_tabs mcp current", tabs2[0].get("current"), True)
check("parse_tabs mcp current_tab_id", ba.current_tab_id(tabs2), "0")

# Пустой результат
check("parse_tabs empty", ba._parse_tabs(""), [])
check("parse_tabs none", ba._parse_tabs("no tabs here"), [])

print("\n[browser_agent._parse_tabs] все проверки пройдены")


# --- server.process_messages с моком OpenRouter -------------------------- #
import server as server_mod

def fake_call(model, user_prompt):
    match = re.search(r"\bid=([^\s]+)", user_prompt)
    mid = match.group(1) if match else "9"
    return json.dumps({
        "jobs": [
            {"messageId": mid, "title": "Senior Angular Developer", "company": "HOS247",
             "location": "Warsaw, Mazowieckie, Poland", "workMode": "fully remote",
             "url": "https://www.linkedin.com/jobs/view/4410073565/",
             "matchedDirection": "Frontend", "matchedStack": "Angular",
             "evidence": "Angular Developer"},
            {"messageId": mid, "title": "React Developer", "company": "HOS247",
             "location": "Warsaw, Poland", "workMode": "remote",
             "url": "", "matchedDirection": "Frontend", "matchedStack": "React",
             "evidence": "React Developer"},
        ]
    })

_real_call_openrouter = server_mod._call_openrouter
server_mod._call_openrouter = fake_call

csv2 = os.path.join(tmp, "telegram2.csv")
orig = server_mod.CSV_PATH
server_mod.CSV_PATH = csv2
try:
    fresh_at = (_dt.now(_tz.utc) - _td(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "9", "text": "Senior Angular Developer and React Developer", "publishedAt": fresh_at,
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


# --- Target direction is enough without matchedStack ---------------------- #
_orig_direction_call = server_mod._call_openrouter
csv_direction = os.path.join(tmp, "telegram_direction.csv")
orig_csv_direction = server_mod.CSV_PATH

def fake_frontend_no_stack(model, user_prompt):
    match = re.search(r"\bid=([^\s]+)", user_prompt)
    mid = match.group(1) if match else "fe1"
    return json.dumps({
        "jobs": [
            {
                "messageId": mid,
                "title": "Senior Frontend Developer",
                "company": "Acme",
                "location": "Remote",
                "workMode": "Remote",
                "url": "",
                "matchedDirection": "Frontend",
                "matchedStack": "",
                "evidence": "Frontend Developer",
            },
        ],
    })

server_mod._call_openrouter = fake_frontend_no_stack
server_mod.CSV_PATH = csv_direction
try:
    lib.reset_csv(csv_direction)
    stats_direction = server_mod.process_messages(
        "https://web.telegram.org/k/#@JobBroadcast",
        [
            {
                "messageId": "fe1",
                "text": "Senior Frontend Developer at Acme. Details by link.",
                "publishedAt": fresh_at,
                "url": "https://t.me/JobBroadcast/1",
                "links": [],
            },
        ],
    )
finally:
    server_mod._call_openrouter = _orig_direction_call
    server_mod.CSV_PATH = orig_csv_direction

check("frontend stackless extracted", stats_direction["jobsExtracted"], 1)
check("frontend stackless rejected", stats_direction["jobsRejected"], 0)
check("frontend stackless rows", stats_direction["rowsAdded"], 1)

row_full_stack, reason_full_stack = server_mod._validated_job_row(
    "https://web.telegram.org/k/#@JobBroadcast",
    server_mod.JobOut(
        messageId="fs2",
        title="Senior Full Stack Developer",
        company="Acme",
        matchedDirection="Full-Stack",
        matchedStack="",
        evidence="Full Stack Developer",
    ),
    {
        "fs2": server_mod.MessageIn(
            messageId="fs2",
            text="Senior Full Stack Developer at Acme. Details by link.",
            publishedAt=fresh_at,
            url="https://t.me/JobBroadcast/2",
            links=[],
        ),
    },
)
check("full stack stackless direction", row_full_stack is not None, True)
check("full stack stackless no reject reason", reason_full_stack, None)

row_backend, reason_backend = server_mod._validated_job_row(
    "https://web.telegram.org/k/#@JobBroadcast",
    server_mod.JobOut(
        messageId="be1",
        title="Backend Engineer",
        company="Acme",
        matchedDirection="Backend",
        matchedStack="",
        evidence="Backend Engineer",
    ),
    {
        "be1": server_mod.MessageIn(
            messageId="be1",
            text="Backend Engineer at Acme. Details by link.",
            publishedAt=fresh_at,
            url="https://t.me/JobBroadcast/3",
            links=[],
        ),
    },
)
check("backend stackless direction", row_backend is not None, True)
check("backend stackless no reject reason", reason_backend, None)

row_plain, reason_plain = server_mod._validated_job_row(
    "https://web.telegram.org/k/#@JobBroadcast",
    server_mod.JobOut(
        messageId="plain1",
        title="Senior Frontend Developer",
        company="Acme",
        matchedDirection="Frontend",
        matchedStack="",
        evidence="Developer role",
    ),
    {
        "plain1": server_mod.MessageIn(
            messageId="plain1",
            text="Developer role at Acme. Details by link.",
            publishedAt=fresh_at,
            url="https://t.me/JobBroadcast/4",
            links=[],
        ),
    },
)
check("plain developer without direction rejected", row_plain, None)
check("plain developer without direction reason", reason_plain, "candidate evidence lacks matched direction or stack")

row_contaminated, reason_contaminated = server_mod._validated_job_row(
    "https://web.telegram.org/k/#@JobBroadcast",
    server_mod.JobOut(
        messageId="multi1",
        title="Project Manager",
        company="BizCo",
        matchedDirection="Frontend",
        matchedStack="",
        evidence="Project Manager",
    ),
    {
        "multi1": server_mod.MessageIn(
            messageId="multi1",
            text="Frontend Developer at Acme\nProject Manager at BizCo",
            publishedAt=fresh_at,
            url="https://t.me/JobBroadcast/5",
            links=[],
        ),
    },
)
check("multi vacancy direction contamination rejected", row_contaminated, None)
check(
    "multi vacancy contamination reason",
    reason_contaminated,
    "candidate evidence lacks matched direction or stack",
)

row_react, reason_react = server_mod._validated_job_row(
    "https://web.telegram.org/k/#@JobBroadcast",
    server_mod.JobOut(
        messageId="multi2",
        title="React Developer",
        company="Acme",
        matchedDirection="Frontend",
        matchedStack="React",
        evidence="React Developer",
    ),
    {
        "multi2": server_mod.MessageIn(
            messageId="multi2",
            text="React Developer at Acme\nFrontend Developer at BizCo",
            publishedAt=fresh_at,
            url="https://t.me/JobBroadcast/6",
            links=[],
        ),
    },
)
check("multi vacancy stack evidence accepted", row_react is not None, True)
check("multi vacancy stack evidence no reject reason", reason_react, None)

def fake_three_jobs_one_message(model, user_prompt):
    return json.dumps({
        "jobs": [
            {
                "messageId": "multi3",
                "title": "React Developer",
                "company": "Acme",
                "location": "",
                "workMode": "Remote",
                "url": "",
                "matchedDirection": "Frontend",
                "matchedStack": "React",
                "evidence": "React Developer",
            },
            {
                "messageId": "multi3",
                "title": "Python Engineer",
                "company": "PyCo",
                "location": "",
                "workMode": "Remote",
                "url": "",
                "matchedDirection": "Backend",
                "matchedStack": "Python",
                "evidence": "Python Engineer",
            },
            {
                "messageId": "multi3",
                "title": "Frontend Developer",
                "company": "WebCo",
                "location": "",
                "workMode": "Remote",
                "url": "",
                "matchedDirection": "Frontend",
                "matchedStack": "",
                "evidence": "Frontend Developer",
            },
        ],
    })

server_mod._call_openrouter = fake_three_jobs_one_message
server_mod.CSV_PATH = csv_direction
try:
    lib.reset_csv(csv_direction)
    stats_multi_jobs = server_mod.process_messages(
        "https://web.telegram.org/k/#@JobBroadcast",
        [
            {
                "messageId": "multi3",
                "text": "React Developer at Acme\nPython Engineer at PyCo\nFrontend Developer at WebCo",
                "publishedAt": fresh_at,
                "url": "https://t.me/JobBroadcast/7",
                "links": [],
            },
        ],
    )
    rows_multi_jobs = lib.read_rows(csv_direction)
finally:
    server_mod._call_openrouter = _orig_direction_call
    server_mod.CSV_PATH = orig_csv_direction

check("one message multiple jobs extracted", stats_multi_jobs["jobsExtracted"], 2)
check("one message multiple jobs rejected", stats_multi_jobs["jobsRejected"], 1)
check("one message multiple jobs rows", stats_multi_jobs["rowsAdded"], 2)
check("one message multiple jobs titles", {r["Title"] for r in rows_multi_jobs}, {"React Developer", "Frontend Developer"})

if os.path.exists(csv_direction):
    os.remove(csv_direction)
if os.path.exists(csv_direction + ".lock"):
    os.remove(csv_direction + ".lock")

print("\n[server] Направление без matchedStack OK")


# --- append_collect_log + process_messages stage logging --------------- #
log_path = os.path.join(tmp, "stage.log")
lib.append_collect_log(log_path, "[@x] test line")
with open(log_path, encoding="utf-8") as f:
    log_lines = f.read().splitlines()
check("append_collect_log writes line", len(log_lines), 1)
check("append_collect_log content", log_lines[0].endswith("[@x] test line"), True)

orig_log = server_mod.COLLECT_LOG_PATH
orig_csv_stage = server_mod.CSV_PATH
csv_stage = os.path.join(tmp, "telegram_stage.csv")
server_mod.COLLECT_LOG_PATH = log_path
server_mod.CSV_PATH = csv_stage
lib.reset_csv(csv_stage)
try:
    stats_log = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "9", "text": "Senior Angular Developer and React Developer", "publishedAt": fresh_at,
             "url": "https://t.me/evacuatejobs/9", "links": []},
        ],
    )
finally:
    server_mod.COLLECT_LOG_PATH = orig_log
    server_mod.CSV_PATH = orig_csv_stage

with open(log_path, encoding="utf-8") as f:
    stage_text = f.read()
check("stage log OpenRouter request", "OpenRouter: запрос" in stage_text, True)
check("stage log OpenRouter response", "OpenRouter: ответ" in stage_text, True)
check("stage log CSV", "CSV: +2 строк" in stage_text, True)
check("stage log jobs parsed", stats_log["jobsExtracted"], 2)

print("\n[server] stage logging OK")


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
check("filter keeps fresh", len(kept), 1)
check("filter drops stale and no-ts", dropped, 2)
check("filter drops no-ts", any(m.text == "no-ts" for m in kept), False)
check("filter drops stale not in kept", any(m.text == "stale" for m in kept), False)

# все старые — все отброшены
kept_all_old, dropped_all_old = server_mod.filter_by_since([stale, stale], 24)
check("filter all old kept", len(kept_all_old), 0)
check("filter all old dropped", dropped_all_old, 2)

# prefilter — широкий, но отсекает очевидный мусор.
check("prefilter Senior Developer", server_mod.should_send_to_model({"text": "Senior Developer"}), True)
check("prefilter ru frontend", server_mod.should_send_to_model({"text": "Фронтенд"}), True)
check("prefilter Angular Developer", server_mod.should_send_to_model({"text": "Angular Developer"}), True)
check("prefilter short JS", server_mod.should_send_to_model({"text": "JS"}), True)
check("prefilter ad noise", server_mod.should_send_to_model({"text": "Большая распродажа и новости недели"}), False)

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
            {"messageId": "1", "text": "Senior Angular Developer and React Developer", "publishedAt": _iso(-1),
             "url": "", "links": []},
            {"messageId": "2", "text": "old Angular Developer", "publishedAt": _iso(-100),
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
            {"messageId": "1", "text": "old Angular Developer 1", "publishedAt": _iso(-100),
             "url": "", "links": []},
            {"messageId": "2", "text": "old React Developer 2", "publishedAt": _iso(-200),
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

# все без метки — отброшены, skipped
csv6 = os.path.join(tmp, "telegram6.csv")
server_mod.CSV_PATH = csv6
try:
    lib.reset_csv(csv6)
    stats6 = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "no ts Angular Developer", "publishedAt": "",
             "url": "", "links": []},
        ],
    )
    check("process no-ts filteredByTime", stats6["filteredByTime"], 1)
    check("process no-ts skipped", bool(stats6.get("skipped", "")), True)
    check("process no-ts no csv rows", len(lib.read_rows(csv6)), 0)
finally:
    server_mod.CSV_PATH = orig_csv
    server_mod.SINCE_HOURS = orig_since
    if os.path.exists(csv6):
        os.remove(csv6)
    if os.path.exists(csv6 + ".lock"):
        os.remove(csv6 + ".lock")

print("\n[server.filter_by_since] все проверки пройдены")


# --- _parse_jobs: messageId как int от модели -------------------------------- #
int_id_response = json.dumps({
    "jobs": [{"messageId": 4295072879, "title": "Dev", "company": "", "location": "",
              "workMode": "Remote", "url": ""}]
})
parsed_int = server_mod._parse_jobs(int_id_response)
check("parse_jobs int messageId", parsed_int[0].messageId, "105583")

loc_response = json.dumps({
    "jobs": [{"messageId": "1", "title": "Dev", "company": "Eshe",
              "location": "Remote", "workMode": "Remote", "url": ""}]
})
parsed_loc = server_mod._parse_jobs(loc_response)
check("parse_jobs strips remote location", parsed_loc[0].location, "")
check("parse_jobs keeps workMode", parsed_loc[0].workMode, "Remote")

print("\n[server._parse_jobs] все проверки пройдены")


# --- OpenRouter request timeout ------------------------------------------- #
class _FakeCompletionResponse:
    choices = [type("Choice", (), {"message": type("Message", (), {"content": '{"jobs": []}'})()})()]


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeCompletionResponse()


class _FakeOpenRouterClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})()


_orig_client = server_mod._client
try:
    _timeout_client = _FakeOpenRouterClient()
    server_mod._client = _timeout_client
    _real_call_openrouter("test-model", "test prompt")
    check("OpenRouter request timeout", _timeout_client.chat.completions.kwargs["timeout"], 25)
finally:
    server_mod._client = _orig_client

print("\n[server OpenRouter timeout] проверка пройдена")

# Встроенные retry OpenAI SDK должны быть выключены: повторные попытки уже
# ограниченно выполняет server._call_openrouter.
import openai as _openai
_orig_openai_ctor = _openai.OpenAI
_orig_server_client = server_mod._client
_client_kwargs = {}


class _FakeOpenAIClient:
    def __init__(self, **kwargs):
        _client_kwargs.update(kwargs)


try:
    _openai.OpenAI = _FakeOpenAIClient
    server_mod._client = None
    server_mod._get_client()
    check("OpenAI SDK retries disabled", _client_kwargs.get("max_retries"), 0)
finally:
    _openai.OpenAI = _orig_openai_ctor
    server_mod._client = _orig_server_client


# --- server chunking + strict validation ---------------------------------- #
chunk_messages = [
    _Msg(messageId=str(i), text=f"Senior Angular Developer {i}", publishedAt=_iso(-1))
    for i in range(20)
]
chunks = server_mod._chunk_messages("https://web.telegram.org/k/#@evacuatejobs", chunk_messages)
check("chunking 20 messages -> 3 chunks", len(chunks), 3)
check("chunking first chunk size", len(chunks[0]), 8)
check("chunking last chunk size", len(chunks[-1]), 4)

_chunk_calls = []

def _fake_empty_chunks(model, user_prompt):
    _chunk_calls.append(user_prompt)
    return json.dumps({"jobs": []})

_orig_call = server_mod._call_openrouter
server_mod._call_openrouter = _fake_empty_chunks
csv_chunks = os.path.join(tmp, "telegram_chunks.csv")
server_mod.CSV_PATH = csv_chunks
try:
    lib.reset_csv(csv_chunks)
    stats_chunks = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": str(i), "text": f"Senior Angular Developer {i}", "publishedAt": _iso(-1),
             "url": "", "links": []}
            for i in range(20)
        ],
    )
    check("process chunk model requests", stats_chunks["modelRequests"], 3)
    check("process chunk call count", len(_chunk_calls), 3)
finally:
    server_mod._call_openrouter = _orig_call
    server_mod.CSV_PATH = orig_csv
    if os.path.exists(csv_chunks):
        os.remove(csv_chunks)
    if os.path.exists(csv_chunks + ".lock"):
        os.remove(csv_chunks + ".lock")

strict_response = json.dumps({
    "jobs": [
        {"messageId": "1", "title": "Senior Angular Developer", "company": "GoodCo",
         "location": "Berlin", "workMode": "Remote",
         "url": "https://jobs.example.com/angular?utm_source=x",
         "matchedDirection": "Frontend", "matchedStack": "Angular",
         "evidence": "Angular Developer"},
        {"messageId": "404", "title": "Senior Angular Developer", "company": "GoodCo",
         "location": "", "workMode": "Remote", "url": "",
         "matchedDirection": "Frontend", "matchedStack": "Angular",
         "evidence": "Angular Developer"},
        {"messageId": "1", "title": "", "company": "GoodCo",
         "location": "", "workMode": "Remote", "url": "",
         "matchedDirection": "Frontend", "matchedStack": "Angular",
         "evidence": "Angular Developer"},
        {"messageId": "1", "title": "Vue Developer", "company": "GoodCo",
         "location": "", "workMode": "Remote", "url": "",
         "matchedDirection": "Frontend", "matchedStack": "Angular",
         "evidence": "Vue Developer"},
        {"messageId": "1", "title": "Python Developer", "company": "GoodCo",
         "location": "", "workMode": "Remote", "url": "",
         "matchedDirection": "Backend", "matchedStack": "Python",
         "evidence": "Angular Developer"},
        {"messageId": "1", "title": "!!!", "company": "",
         "location": "", "workMode": "Remote", "url": "",
         "matchedDirection": "Frontend", "matchedStack": "Angular",
         "evidence": "Angular Developer"},
        {"messageId": "1", "title": "React Developer", "company": "GoodCo",
         "location": "Berlin", "workMode": "Remote",
         "url": "https://evil.test/job",
         "matchedDirection": "Frontend", "matchedStack": "React",
         "evidence": "React Developer"},
    ]
})

def _fake_strict(model, user_prompt):
    return strict_response

server_mod._call_openrouter = _fake_strict
csv_strict = os.path.join(tmp, "telegram_strict.csv")
server_mod.CSV_PATH = csv_strict
try:
    lib.reset_csv(csv_strict)
    stats_strict = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "Senior Angular Developer and React Developer at GoodCo",
             "publishedAt": _iso(-1), "url": "https://t.me/evacuatejobs/1",
             "links": ["https://jobs.example.com/angular"]},
        ],
    )
    rows_strict = lib.read_rows(csv_strict)
    strict_urls = {r["URL"] for r in rows_strict}
    check("strict accepted jobs", stats_strict["jobsExtracted"], 2)
    check("strict rejected jobs", stats_strict["jobsRejected"], 5)
    check("strict rows added", stats_strict["rowsAdded"], 2)
    check("strict keeps linked url", "https://jobs.example.com/angular" in strict_urls, True)
    check("strict arbitrary url falls back", "https://t.me/evacuatejobs/1" in strict_urls, True)
finally:
    server_mod._call_openrouter = fake_call
    server_mod.CSV_PATH = orig_csv
    if os.path.exists(csv_strict):
        os.remove(csv_strict)
    if os.path.exists(csv_strict + ".lock"):
        os.remove(csv_strict + ".lock")

print("\n[server chunking + strict validation] все проверки пройдены")


# --- AI diagnostics ------------------------------------------------------- #
_diag_env_keys = [
    "AI_DIAGNOSTICS_ENABLED",
    "AI_DIAGNOSTICS_RUN_ID",
    "OPENROUTER_API_KEY",
]
_diag_env_orig = {k: os.environ.get(k) for k in _diag_env_keys}
_orig_diag_dir = diagnostics.DIAGNOSTICS_DIR
_orig_diag_call = server_mod._call_openrouter
_orig_diag_csv = server_mod.CSV_PATH
_orig_diag_since = server_mod.SINCE_HOURS
try:
    disabled_dir = os.path.join(tmp, "diag_disabled")
    diagnostics.DIAGNOSTICS_DIR = Path(disabled_dir)
    os.environ["AI_DIAGNOSTICS_ENABLED"] = "false"
    diagnostics.write_log("messages", {"source": "x", "stage": "test"})
    check("diagnostics disabled no file", os.path.exists(os.path.join(disabled_dir, "messages.jsonl")), False)

    secret_value = "test-secret-value-123456"
    os.environ["OPENROUTER_API_KEY"] = secret_value
    sanitized = diagnostics.sanitize({
        "api_key": secret_value,
        "text": "Senior token engineer role",
        "nested": {"authorization": "Bearer abcdefghijklmnop"},
        "url": "https://example.test/path?token=abcdefghijklmnop&ok=1",
    })
    check("diagnostics sanitizer key", sanitized["api_key"], "[REDACTED]")
    check("diagnostics sanitizer normal text", sanitized["text"], "Senior token engineer role")
    check("diagnostics sanitizer auth", sanitized["nested"]["authorization"], "[REDACTED]")
    check("diagnostics sanitizer query", "token=[REDACTED]" in sanitized["url"], True)

    diag_dir = os.path.join(tmp, "diag_enabled")
    diagnostics.DIAGNOSTICS_DIR = Path(diag_dir)
    os.environ["AI_DIAGNOSTICS_ENABLED"] = "true"
    os.environ["AI_DIAGNOSTICS_RUN_ID"] = "test-run"
    os.makedirs(diag_dir, exist_ok=True)
    stale_path = os.path.join(diag_dir, "messages.jsonl")
    with open(stale_path, "w", encoding="utf-8") as f:
        f.write("{}\n")
    diagnostics.clean_start()
    check("diagnostics clean start removes stale", os.path.exists(stale_path), False)

    diag_response = json.dumps({
        "jobs": [
            {"messageId": "1", "title": "Senior Angular Developer", "company": "GoodCo",
             "location": "Berlin", "workMode": "Remote", "url": "",
             "matchedDirection": "Frontend", "matchedStack": "Angular",
             "evidence": "Angular Developer"},
            {"messageId": "1", "title": "Senior Angular Developer", "company": "GoodCo",
             "location": "Berlin", "workMode": "Remote", "url": "",
             "matchedDirection": "Frontend", "matchedStack": "Angular",
             "evidence": "Angular Developer"},
            {"messageId": "1", "title": "", "company": "GoodCo",
             "location": "Berlin", "workMode": "Remote", "url": "",
             "matchedDirection": "Frontend", "matchedStack": "Angular",
             "evidence": "Angular Developer"},
        ]
    })

    def _fake_diag(model, user_prompt):
        return diag_response

    server_mod._call_openrouter = _fake_diag
    server_mod.CSV_PATH = os.path.join(tmp, "telegram_diag.csv")
    server_mod.SINCE_HOURS = 24
    lib.reset_csv(server_mod.CSV_PATH)
    stats_diag = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "Senior Angular Developer at GoodCo", "publishedAt": _iso(-1),
             "url": "https://t.me/evacuatejobs/1", "links": []},
            {"messageId": "2", "text": "Большая распродажа и новости недели", "publishedAt": _iso(-1),
             "url": "https://t.me/evacuatejobs/2", "links": []},
            {"messageId": "3", "text": "old Angular Developer", "publishedAt": _iso(-100),
             "url": "https://t.me/evacuatejobs/3", "links": []},
        ],
    )
    check("diagnostics stats rejected", stats_diag["jobsRejected"], 1)
    check("diagnostics stats duplicates", stats_diag["duplicates"], 1)
    check("diagnostics stats rows", stats_diag["rowsAdded"], 1)

    msg_records = read_jsonl(os.path.join(diag_dir, "messages.jsonl"))
    req_records = read_jsonl(os.path.join(diag_dir, "ai_requests.jsonl"))
    resp_records = read_jsonl(os.path.join(diag_dir, "ai_responses.jsonl"))
    job_records = read_jsonl(os.path.join(diag_dir, "jobs.jsonl"))
    check("diagnostics message time pass", any(r.get("reasonCode") == "passed_time_filter" for r in msg_records), True)
    check("diagnostics message time reject", any(r.get("reasonCode") == "older_than_since_hours" for r in msg_records), True)
    check("diagnostics message prefilter pass", any(r.get("reasonCode") == "passed_prefilter" for r in msg_records), True)
    check("diagnostics message prefilter reject", any(r.get("reasonCode") == "rejected_prefilter" for r in msg_records), True)
    check("diagnostics message ai sent", any(r.get("reasonCode") == "sent_to_ai" for r in msg_records), True)
    check("diagnostics request messages", any("Senior Angular Developer" in r["messages"][1]["content"] for r in req_records), True)
    check("diagnostics response raw", any(r.get("rawResponse") == diag_response for r in resp_records), True)
    check("diagnostics job validation accepted", any(r.get("stage") == "validation" and r.get("outcome") == "accepted" for r in job_records), True)
    check("diagnostics job validation rejected", any(r.get("stage") == "validation" and r.get("reasonCode") == "empty_title" for r in job_records), True)
    check("diagnostics job dedup added", any(r.get("stage") == "deduplication" and r.get("outcome") == "added" for r in job_records), True)
    check("diagnostics job dedup duplicate", any(r.get("stage") == "deduplication" and r.get("outcome") == "duplicate" for r in job_records), True)

    diagnostics.clean_start()

    def _fake_bad_json(model, user_prompt):
        return "not json"

    server_mod._call_openrouter = _fake_bad_json
    lib.reset_csv(server_mod.CSV_PATH)
    stats_bad = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "Senior Angular Developer", "publishedAt": _iso(-1),
             "url": "https://t.me/evacuatejobs/1", "links": []},
        ],
    )
    bad_jobs = read_jsonl(os.path.join(diag_dir, "jobs.jsonl"))
    bad_responses = read_jsonl(os.path.join(diag_dir, "ai_responses.jsonl"))
    check("diagnostics parse failure errors", bool(stats_bad["errors"]), True)
    check("diagnostics parse failure raw response", any(r.get("rawResponse") == "not json" for r in bad_responses), True)
    check("diagnostics parse failure job event", any(r.get("stage") == "parsing" and r.get("rawResponse") == "not json" for r in bad_jobs), True)

    with open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
        gitignore_text = f.read()
    check("gitignore diagnostics dir", "diagnostics/" in gitignore_text, True)
finally:
    diagnostics.DIAGNOSTICS_DIR = _orig_diag_dir
    server_mod._call_openrouter = _orig_diag_call
    server_mod.CSV_PATH = _orig_diag_csv
    server_mod.SINCE_HOURS = _orig_diag_since
    for _k, _v in _diag_env_orig.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

print("\n[diagnostics] все проверки пройдены")


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
ba._merge_by_id(acc, [{"messageId": "a", "text": "x", "publishedAt": fresh_iso}])
check("merge duplicate fills timestamp", acc["a"]["publishedAt"], fresh_iso)

# _scroll_js
check("scroll top js has scrollTop=0", "scrollTop = 0" in ba._scroll_js("top"), True)
check("scroll bottom js has scrollHeight", "scrollHeight" in ba._scroll_js("bottom"), True)
check("scroll state parser bottom", ba._scroll_at_bottom_from_eval('{"found":true,"atBottom":true}'), True)
check(
    "scroll state parser numeric",
    ba._scroll_at_bottom_from_eval('{"found":true,"scrollTop":90,"clientHeight":100,"scrollHeight":195}'),
    True,
)
check("scroll state parser missing", ba._scroll_at_bottom_from_eval('{"found":false}'), None)

# _batch_summary
summary = ba._batch_summary([
    {"messageId": "a", "publishedAt": old_iso},
    {"messageId": "b", "publishedAt": fresh_iso},
])
check("batch summary count", summary.startswith("batch=2, ts="), True)
check("batch summary ids", summary.endswith(", ids=a..b"), True)

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
    def __init__(self, extract_responses, at_bottom=None):
        self._r = list(extract_responses)
        self._i = 0
        self.scroll_count = 0
        self.extract_count = 0
        self.at_bottom = at_bottom

    async def call(self, tool, args, timeout=None):
        if tool != "browser_evaluate":
            return ""
        fn = args.get("function", "")
        if "atBottom" in fn and "clientHeight" in fn and "scrollHeight" in fn:
            if self.at_bottom is None:
                return json.dumps({"found": False, "atBottom": False})
            return json.dumps({
                "found": True,
                "atBottom": self.at_bottom,
                "scrollTop": 190 if self.at_bottom else 0,
                "clientHeight": 100,
                "scrollHeight": 290,
            })
        if "el.scrollTop =" in fn:
            self.scroll_count += 1
            return "ok"
        r = self._r[min(self._i, len(self._r) - 1)]
        self._i += 1
        self.extract_count += 1
        return r


# Сценарий 1: приземлились на старых сообщениях, скроллим к новым, потом к старым.
#   extract 0 (initial): 2 old messages
#   phase 1 scroll newer -> extract 1: 2 fresh messages
#   phase 1 scroll newer -> extract 2: same fresh (no new -> stop)
#   phase 2 starts from the current batch
#   phase 2 scroll older -> extract 3: same fresh (no new -> stop)
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

fake2_log = _FakeMCP(extracts)
stderr_buf = io.StringIO()
with contextlib.redirect_stderr(stderr_buf):
    _asyncio.run(ba.collect_with_scroll(fake2_log, "https://web.telegram.org/k/#@t", 24))
scroll_log = stderr_buf.getvalue()
check("scroll log initial count", "scroll initial: batch=2" in scroll_log, True)
check("scroll log new count", "scroll phase1 iter=0: batch=2" in scroll_log and "new=2" in scroll_log, True)
check("scroll log accumulated total", "total=4" in scroll_log, True)

# Сценарий 3: приземлились на свежих сообщениях (хвост), старых нет.
#   extract 0 (initial): 2 fresh
#   phase 1: newest_ts fresh -> break immediately
#   phase 2: oldest_ts fresh (not < cutoff), scroll older
#   phase 2 -> extract 1: same fresh (no new -> stop)
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
res3 = _asyncio.run(ba.collect_with_scroll(
    fake3, "https://web.telegram.org/k/#@t", 24, tail_tolerance_s=7200, wait_ms=0
))
check("scroll at tail returns 2", len(res3), 2)
check("scroll at tail sorted", res3[0]["messageId"], "c")
check("scroll at tail retries stale older edge", fake3.scroll_count >= 2, True)

# Сценарий 3a: первая прокрутка к старым не даёт новых DOM-сообщений,
# следующая прокрутка догружает более раннее сообщение в окне since_hours.
extracts3a = [
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
        {"messageId": "d", "text": "new2", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "b", "text": "older", "publishedAt": _iso(-2), "url": "", "links": []},
        {"messageId": "c", "text": "new1", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
]
fake3a = _FakeMCP(extracts3a)
res3a = _asyncio.run(ba.collect_with_scroll(
    fake3a, "https://web.telegram.org/k/#@JobBroadcast", 24,
    tail_tolerance_s=7200, wait_ms=0, max_iter=6,
))
check("scroll phase2 ignores first no-new scroll", any(m["messageId"] == "b" for m in res3a), True)
check("scroll phase2 repeated older scroll", fake3a.scroll_count >= 2, True)

# Сценарий 3b: редкий канал уже внизу, но последние сообщения старые.
fake3b = _FakeMCP([
    _msg_json([
        {"messageId": "a", "text": "old1", "publishedAt": old_iso, "url": "", "links": []},
        {"messageId": "b", "text": "old2", "publishedAt": old_iso, "url": "", "links": []},
    ]),
], at_bottom=True)
res3b = _asyncio.run(ba.collect_with_scroll(fake3b, "https://web.telegram.org/k/#@t", 24))
check("scroll old at bottom returns messages", len(res3b), 2)
check("scroll old at bottom skips phase1 scroll", fake3b.scroll_count, 0)

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

# Сценарий 6: Telegram сначала отдаёт карточку до появления timestamp,
# затем тот же messageId с уже заполненной датой.
extracts6 = [
    _msg_json([
        {"messageId": "m1", "text": "same", "publishedAt": "", "url": "", "links": []},
    ]),
    _msg_json([
        {"messageId": "m1", "text": "same", "publishedAt": fresh_iso, "url": "", "links": []},
    ]),
]
res6 = _asyncio.run(ba.collect_with_scroll(
    _FakeMCP(extracts6), "https://web.telegram.org/k/#@t", 24,
    tail_tolerance_s=7200, wait_ms=0, max_iter=2,
))
check("scroll duplicate message count", len(res6), 1)
check("scroll duplicate fills timestamp", res6[0]["publishedAt"], fresh_iso)

# Сценарий 9: max_messages обрезает результат до N самых новых.
_many_msgs = [
    {"messageId": str(i), "text": f"m{i}", "publishedAt": fresh_iso, "url": "", "links": []}
    for i in range(50)
]

class _FakeMCPMany:
    def __init__(self):
        self.scroll_count = 0

    async def call(self, tool, args, timeout=None):
        if tool != "browser_evaluate":
            return ""
        fn = args.get("function", "")
        if "atBottom" in fn and "clientHeight" in fn and "scrollHeight" in fn:
            return json.dumps({"found": False, "atBottom": False})
        if "el.scrollTop =" in fn:
            self.scroll_count += 1
            return "ok"
        return _msg_json(_many_msgs)

res9 = _asyncio.run(ba.collect_with_scroll(
    _FakeMCPMany(), "https://web.telegram.org/k/#@t", 24, max_messages=10,
))
check("messages limit trims to 10", len(res9), 10)
check("messages limit keeps newest", res9[-1]["messageId"], "49")

print("\n[browser_agent.collect_with_scroll] все проверки пройдены")


# --- script_tab_ids_to_close (R2) ----------------------------------------- #
tabs_at_start = [
    {"tabId": "1", "url": "chrome-extension://abc/connect.html"},
]
tabs_after = [
    {"tabId": "1", "url": "chrome-extension://abc/connect.html"},
    {"tabId": "2", "url": "chrome-extension://abc/connect.html"},
]
check("tab close diff", ba.script_tab_ids_to_close(tabs_at_start, tabs_after), {"2"})
check("tab close pre-existing connect", ba.script_tab_ids_to_close(
    tabs_at_start, tabs_at_start,
), set())
tabs_user_start = [
    {"tabId": "5", "url": "https://example.com/"},
    {"tabId": "6", "url": "https://other.com/"},
    {"tabId": "7", "url": "chrome-extension://abc/connect.html", "current": True},
]
tabs_user_final = [
    {"tabId": "5", "url": "https://example.com/"},
    {"tabId": "6", "url": "https://other.com/"},
    {"tabId": "7", "url": "https://web.telegram.org/k/#@test"},
]
check("tab close working connect", ba.script_tab_ids_to_close(
    tabs_user_start, tabs_user_final, working_tab_id="7",
), {"7"})

print("\n[browser_agent.script_tab_ids_to_close] все проверки пройдены")


# --- async submit timeout (collect pattern) -------------------------------- #
def _slow_submit():
    _time.sleep(0.2)
    return {}

try:
    _asyncio.run(_asyncio.wait_for(_asyncio.to_thread(_slow_submit), timeout=0.05))
    check("async submit timeout raises", False, True)
except _asyncio.TimeoutError:
    check("async submit timeout raises", True, True)

print("\n[collect.async submit] все проверки пройдены")


# --- collect._source_outcome ------------------------------------------------ #
import collect as collect_mod

check(
    "submit task timeout exceeds HTTP timeout",
    collect_mod.SUBMIT_TASK_TIMEOUT > collect_mod.SUBMIT_HTTP_TIMEOUT,
    True,
)

check("outcome success", collect_mod._source_outcome({"errors": [], "skipped": ""}), "success")
check("outcome skipped", collect_mod._source_outcome({"skipped": "all old"}), "skipped")
check("outcome errored", collect_mod._source_outcome({"errors": ["x"], "skipped": ""}), "errored")
check(
    "outcome errors beat skipped",
    collect_mod._source_outcome({"errors": ["x"], "skipped": "all old"}),
    "errored",
)
check(
    "zero scrape skip constant",
    collect_mod.NO_MESSAGES_SKIP,
    "Нет сообщений на странице",
)
check(
    "zero scrape outcome",
    collect_mod._source_outcome({"skipped": collect_mod.NO_MESSAGES_SKIP}),
    "empty",
)

_select_sample = [
    "https://web.telegram.org/k/#@JobBroadcast",
    "https://web.telegram.org/k/#@evacuatejobs",
]
check(
    "select channel by @name",
    collect_mod.select_channels(_select_sample, "@JobBroadcast"),
    ["https://web.telegram.org/k/#@JobBroadcast"],
)
check(
    "select channel by bare name",
    collect_mod.select_channels(_select_sample, "JobBroadcast"),
    ["https://web.telegram.org/k/#@JobBroadcast"],
)
check(
    "select channel empty keeps all",
    collect_mod.select_channels(_select_sample, ""),
    _select_sample,
)
try:
    collect_mod.select_channels(_select_sample, "@missing")
    check("select missing channel raises", False, True)
except ValueError as e:
    check("select missing channel raises", "channels.json" in str(e), True)

print("\n[collect._source_outcome] все проверки пройдены")


# --- collect session-check policy + worker queue -------------------------- #
class _FakeMCPNav:
    def __init__(self, fail_nav=False):
        self.fail_nav = fail_nav
        self.calls = []

    async def call(self, tool, args, timeout=None):
        self.calls.append((tool, args))
        if self.fail_nav and tool == "browser_navigate":
            raise RuntimeError("nav failed")
        return ""


_session_calls = []
_ready_values = []

async def _fake_ready(mcp):
    return _ready_values.pop(0) if _ready_values else True


async def _fake_session(mcp):
    _session_calls.append(True)
    return True


async def _fake_collect(mcp, source_url, since_hours):
    return []


_orig_ready = collect_mod.ba.wait_for_channel_ready
_orig_session = collect_mod.ba.telegram_session_active
_orig_collect_scroll = collect_mod.ba.collect_with_scroll
try:
    collect_mod.ba.wait_for_channel_ready = _fake_ready
    collect_mod.ba.telegram_session_active = _fake_session
    collect_mod.ba.collect_with_scroll = _fake_collect
    collect_mod.STATS["per_source"] = {}
    _session_calls.clear()
    _ready_values[:] = [True, True, False]
    _asyncio.run(collect_mod.process_source(_FakeMCPNav(), "https://web.telegram.org/k/#@one", check_session=True))
    _asyncio.run(collect_mod.process_source(_FakeMCPNav(), "https://web.telegram.org/k/#@two", check_session=False))
    _asyncio.run(collect_mod.process_source(_FakeMCPNav(), "https://web.telegram.org/k/#@three", check_session=False))
    nav_result = _asyncio.run(collect_mod.process_source(
        _FakeMCPNav(fail_nav=True),
        "https://web.telegram.org/k/#@four",
        check_session=False,
    ))
    check("session first + empty dom only", len(_session_calls), 2)
    check("navigation error asks next session check", nav_result["session_check_next"], True)
finally:
    collect_mod.ba.wait_for_channel_ready = _orig_ready
    collect_mod.ba.telegram_session_active = _orig_session
    collect_mod.ba.collect_with_scroll = _orig_collect_scroll


def _run_worker_queue_test():
    async def _run():
        orig_submit = collect_mod.submit_messages
        try:
            def _fake_submit(source, messages, timeout=90):
                _time.sleep(0.05)
                return {
                    "source": source,
                    "messagesReceived": len(messages),
                    "filteredByTime": 0,
                    "filteredByPrefilter": 0,
                    "modelRequests": 1,
                    "jobsExtracted": len(messages),
                    "jobsRejected": 0,
                    "rowsAdded": len(messages),
                    "duplicates": 0,
                    "skipped": "",
                    "errors": [],
                }

            collect_mod.submit_messages = _fake_submit
            collect_mod.STATS["per_source"] = {}
            queue = _asyncio.Queue()
            workers = [
                _asyncio.create_task(collect_mod._openrouter_worker(i, queue))
                for i in range(2)
            ]
            for i in range(3):
                src = f"https://web.telegram.org/k/#@q{i}"
                await queue.put((src, [{"messageId": str(i)}], collect_mod._empty_source_stats(src)))
            await queue.join()
            for _ in workers:
                await queue.put(None)
            await _asyncio.gather(*workers)
            return collect_mod.STATS["per_source"]
        finally:
            collect_mod.submit_messages = orig_submit

    return _asyncio.run(_run())


worker_stats = _run_worker_queue_test()
check("worker queue processed all", len(worker_stats), 3)
check("worker queue rows added", sum(st["rowsAdded"] for st in worker_stats.values()), 3)

print("\n[collect session policy + worker queue] все проверки пройдены")


# --- collector sends unfiltered messages to server ------------------------- #
csv_col = os.path.join(tmp, "telegram_collector.csv")
server_mod.CSV_PATH = csv_col
server_mod.SINCE_HOURS = 24
try:
    lib.reset_csv(csv_col)
    collector_resp = server_mod.process_messages(
        "https://web.telegram.org/k/#@evacuatejobs",
        [
            {"messageId": "1", "text": "Senior Angular Developer and React Developer", "publishedAt": _iso(-1),
             "url": "", "links": []},
            {"messageId": "2", "text": "old Angular Developer", "publishedAt": _iso(-100),
             "url": "", "links": []},
        ],
    )
    check("collector path messagesReceived", collector_resp["messagesReceived"], 2)
    check("collector path filteredByTime", collector_resp["filteredByTime"], 1)
    check("collector path not skipped", collector_resp.get("skipped", ""), "")
finally:
    server_mod.CSV_PATH = orig_csv
    server_mod.SINCE_HOURS = orig_since
    if os.path.exists(csv_col):
        os.remove(csv_col)
    if os.path.exists(csv_col + ".lock"):
        os.remove(csv_col + ".lock")

print("\n[collect.server stats merge] все проверки пройдены")


# --- browser_agent.wait_for_channel_ready ---------------------------------- #
class _FakeMCPReady:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def call(self, tool, args, timeout=None):
        self.calls += 1
        if tool != "browser_evaluate":
            return ""
        idx = min(self.calls - 1, len(self.responses) - 1)
        return self.responses[idx]


async def _run_ready_early():
    mcp = _FakeMCPReady([
        '{"bubbles":0,"scrollable":false}',
        '{"bubbles":2,"scrollable":true}',
    ])
    t0 = _time.monotonic()
    ok = await ba.wait_for_channel_ready(mcp, timeout_s=2, poll_s=0.05)
    elapsed = _time.monotonic() - t0
    return ok, mcp.calls, elapsed


ok_early, calls_early, elapsed_early = _asyncio.run(_run_ready_early())
check("wait_for_channel_ready early", ok_early, True)
check("wait_for_channel_ready early calls", calls_early, 2)
check("wait_for_channel_ready early fast", elapsed_early < 1.0, True)


async def _run_ready_timeout():
    mcp = _FakeMCPReady(['{"bubbles":0,"scrollable":false}'])
    ok = await ba.wait_for_channel_ready(mcp, timeout_s=0.15, poll_s=0.05)
    return ok, mcp.calls


ok_timeout, calls_timeout = _asyncio.run(_run_ready_timeout())
check("wait_for_channel_ready timeout ok", ok_timeout, False)
check("wait_for_channel_ready timeout polled", calls_timeout >= 1, True)

print("\n[browser_agent.wait_for_channel_ready] все проверки пройдены")


# --- server endpoint passes all messages before time filter --------------- #
_captured_msg_count = []

def _fake_process_messages(source, messages):
    _captured_msg_count.append(len(messages))
    return {
        "source": source,
        "messagesReceived": len(messages),
        "filteredByTime": 0,
        "jobsExtracted": 0,
        "rowsAdded": 0,
        "duplicates": 0,
        "skipped": "",
        "errors": [],
    }

_orig_process = server_mod.process_messages
_orig_server_token = server_mod.SERVER_AUTH_TOKEN
server_mod.process_messages = _fake_process_messages
server_mod.SERVER_AUTH_TOKEN = "test-server-token"
try:
    with server_mod.app.test_client() as client:
        payload = {
            "source": "https://web.telegram.org/k/#@evacuatejobs",
            "messages": [
                {"messageId": str(i), "text": f"m{i}", "publishedAt": fresh_at,
                 "url": "", "links": []}
                for i in range(20)
            ],
        }
        unauthorized = client.post("/import-telegram", json=payload)
        check("endpoint rejects missing token", unauthorized.status_code, 401)
        resp = client.post(
            "/import-telegram",
            json=payload,
            headers={"Authorization": "Bearer test-server-token"},
        )
        check("endpoint status 200", resp.status_code, 200)
        check("endpoint passes all messages", _captured_msg_count[-1], 20)
        check(
            "endpoint request limit configured",
            server_mod.app.config["MAX_CONTENT_LENGTH"],
            2 * 1024 * 1024,
        )
finally:
    server_mod.process_messages = _orig_process
    server_mod.SERVER_AUTH_TOKEN = _orig_server_token

print("\n[server] import-telegram message count OK")


# --- collect.log append-only (no race with server) ----------------------- #
race_log = os.path.join(tmp, "race.log")
_saved_stdout = sys.stdout
_saved_stderr = sys.stderr
try:
    lib.setup_file_logging(race_log)
    print("parent-before")
    lib.append_collect_log(race_log, "[server] server-stage")
    print("parent-after")
    sys.stdout.flush()
finally:
    sys.stdout = _saved_stdout
    sys.stderr = _saved_stderr

with open(race_log, encoding="utf-8") as f:
    race_text = f.read()
check("race server line intact", "[server] server-stage" in race_text, True)
check("race parent-after intact", "parent-after" in race_text, True)
check("race no truncated server line", "erver-stage" in race_text and "[server] server-stage" not in race_text, False)

print("\n[collect.log race] все проверки пройдены")


# --- collect.py logging --------------------------------------------------- #
with open(os.path.join(HERE, "collect.py"), encoding="utf-8") as _cf:
    _collect_src = _cf.read()
check("no client time filter", "_filter_dated_messages" in _collect_src, False)
check(
    "process_source combined count log",
    "отфильтровано по времени" in _collect_src and "Собрано сообщений" in _collect_src,
    True,
)

print("\n[collect.logging] все проверки пройдены")
