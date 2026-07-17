@echo off
rem ===========================================================================
rem Запуск сбора вакансий из Telegram Web через Playwright MCP (Extension).
rem Требования:
rem  - Python 3.11+
rem  - Node.js 18+ и npx
rem  - Google Chrome запущен, в нём установлено Playwright Extension,
rem    в .env заданы OPENROUTER_API_KEY и PLAYWRIGHT_MCP_EXTENSION_TOKEN.
rem  - Chrome не закрывается скриптом; пользовательские вкладки не трогаются.
rem    Вкладки, открытые скриптом (connect.html), закрываются при выходе.
rem ===========================================================================
cd /d "%~dp0"

rem --- Проверка Python ---
where python >nul 2>nul
if errorlevel 1 (
  echo ОШИБКА: Python не найден в PATH.
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo ОШИБКА: Требуется Python 3.11 или новее.
  pause
  exit /b 1
)

rem --- Проверка Node.js ---
where node >nul 2>nul
if errorlevel 1 (
  echo ОШИБКА: Node.js не найден в PATH.
  pause
  exit /b 1
)
node -e "const v=process.versions.node.split('.').map(Number); if(v[0]<18){process.exit(1)}" 2>nul
if errorlevel 1 (
  echo ОШИБКА: Требуется Node.js 18 или новее.
  pause
  exit /b 1
)

rem --- Проверка npx ---
where npx >nul 2>nul
if errorlevel 1 (
  echo ОШИБКА: npx не найден в PATH.
  pause
  exit /b 1
)

rem --- Проверка обязательных файлов ---
for %%f in (collect.py server.py browser_agent.py lib.py prompt.md channels.json playwright-mcp.json) do (
  if not exist "%%f" (
    echo ОШИБКА: отсутствует обязательный файл %%f
    pause
    exit /b 1
  )
)

rem --- Проверка переменных окружения (.env) ---
python -c "import os; from dotenv import load_dotenv; load_dotenv(); k=os.getenv('OPENROUTER_API_KEY',''); t=os.getenv('PLAYWRIGHT_MCP_EXTENSION_TOKEN',''); import sys; sys.exit(0 if k and t else 1)" 2>nul
if errorlevel 1 (
  echo ОШИБКА: в .env должны быть заданы OPENROUTER_API_KEY и PLAYWRIGHT_MCP_EXTENSION_TOKEN.
  pause
  exit /b 1
)

echo [@] Запуск Telegram Jobs Collector...
python collect.py %*
set RC=%errorlevel%

if "%RC%"=="130" (
  echo [+] Завершено по Ctrl+C. Дочерние процессы остановлены, Chrome не закрыт.
) else if not "%RC%"=="0" (
  echo [!] collect.py завершился с кодом %RC%.
  pause
)

exit /b %RC%
