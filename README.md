# Telegram Jobs Collector

Collects recent posts from configured Telegram Web sources through the Playwright MCP Chrome extension, asks OpenRouter to extract matching software jobs, and writes the results to `telegram.csv`.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer with npm
- Google Chrome with the Playwright MCP extension
- An active Telegram Web session in Chrome
- An OpenRouter API key

## Quick start

On macOS, run the setup command, fill in the two generated private configuration files, then run the collector:

```sh
./setup-macos.sh
open -t .env channels.json
.venv/bin/python run.py
```

`channels.json` is a JSON array of Telegram Web URLs:

```json
[
  "https://web.telegram.org/k/#@example_channel"
]
```

## macOS setup

`./setup-macos.sh` checks Python, Node.js, npm, and Chrome; creates `.venv`; installs locked Node dependencies and Python requirements; and creates `.env` and `channels.json` only when they do not exist. It does not install or configure the Chrome extension.

The script is architecture-neutral and uses the native Python and Node.js already installed on Apple Silicon or Intel Macs.

## Required environment variables

Store these in the ignored `.env` file:

- `OPENROUTER_API_KEY`: OpenRouter API key.
- `PLAYWRIGHT_MCP_EXTENSION_TOKEN`: token shown by the Playwright MCP Chrome extension.

Optional runtime settings use safe defaults in the code.

## Main commands

```sh
.venv/bin/python run.py                     # collect all configured sources
.venv/bin/python run.py --channel @channel  # collect one configured source
.venv/bin/python run.py --selftest           # offline regression suite
```

On Windows, after installing the dependencies, use `start-telegram-jobs.bat`.

## Important limitations

- Chrome must already be running with an authenticated Telegram Web session and the Playwright MCP extension connected.
- Collection relies on Telegram Web DOM selectors and may need a code update when Telegram changes its page structure.
- Telegram message content is sent to the configured OpenRouter model.
- Each run replaces `telegram.csv`; sources are processed sequentially in one browser tab.

## Troubleshooting

- “session not found”: sign in at `https://web.telegram.org/k/` in the same Chrome profile as the extension.
- MCP connection failure: confirm the extension token in `.env`, then restart Chrome and the extension.
- Empty results: verify `channels.json`, recent message timestamps, and `TELEGRAM_SINCE_HOURS` if you override it.
- Setup failure: install the missing version reported by `setup-macos.sh` and rerun the same command.

## Security notes

`.env`, `channels.json`, CSV output, logs, diagnostics, browser state, caches, and dependencies are ignored by Git. Keep diagnostics disabled unless needed: they contain message and model data. The collector executes only its fixed browser extraction code and authenticates its loopback HTTP server with a per-run token.
