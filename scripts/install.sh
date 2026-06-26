#!/usr/bin/env bash
# refurb_watcher · install (SQLite backend — no external services required)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> refurb_watcher install in $PROJECT_DIR"

# 1. virtualenv
if [ ! -d venv ]; then
    echo "==> Creating Python venv"
    python3 -m venv venv
fi

# 2. dependencies
echo "==> Installing dependencies"
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 3. config
if [ ! -f config.toml ]; then
    echo "==> Copying config.toml.example -> config.toml"
    cp config.toml.example config.toml
    echo "    ⚠ Edit config.toml (URLs to watch, filters, optional ntfy)"
fi

# 4. logs dir
mkdir -p logs

echo
echo "==> Done. Next steps:"
echo "  1. Edit config.toml        (defaults work out of the box with SQLite)"
echo "  2. First run:              ./venv/bin/python watcher.py"
echo "  3. Schedule polling:       crontab -e   # see scripts/crontab.example"
echo "  4. Dashboard:              ./venv/bin/python web.py   # http://127.0.0.1:5060"
echo "                             (or scripts/refurb_watcher.service for systemd)"
echo
echo "  Prefer MariaDB? set [database] backend = \"mariadb\" in config.toml,"
echo "  run './venv/bin/pip install pymysql', then apply schema.sql once."
echo
echo "✓ Install OK"
