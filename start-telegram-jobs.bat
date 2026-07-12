@echo off
rem Запуск сбора вакансий из Telegram Web.
rem Требования:
rem  - Google Chrome запущен с включённой удалённой отладкой
rem    (chrome://inspect/#remote-debugging) и активной сессией Telegram Web;
rem  - в .env задан OPENROUTER_API_KEY.
cd /d "%~dp0"
python collect.py
pause
