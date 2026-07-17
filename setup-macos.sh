#!/bin/sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "error: setup-macos.sh must be run on macOS" >&2
  exit 1
fi

for tool in python3 node npm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: required tool '$tool' was not found in PATH" >&2
    exit 1
  fi
done

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "error: Python 3.11 or newer is required" >&2
  exit 1
}

node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' || {
  echo "error: Node.js 20 or newer is required" >&2
  exit 1
}

if ! open -Ra "Google Chrome"; then
  echo "error: Google Chrome was not found" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "error: existing .venv does not use Python 3.11 or newer" >&2
  exit 1
}

.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
npm ci --ignore-scripts

if [ ! -f .env ]; then
  (umask 077 && cp .env.example .env)
  echo "created .env; add the required tokens before running"
fi

if [ ! -f channels.json ]; then
  (umask 077 && printf '%s\n' '[]' > channels.json)
  echo "created channels.json; add at least one Telegram Web source"
fi

echo "setup complete"
echo "run: .venv/bin/python run.py"
