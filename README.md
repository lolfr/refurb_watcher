# refurb_watcher

Watch Apple Refurbished store pages and get notified the moment a machine you
care about appears — with a small web dashboard showing what's currently
available plus the full appearance / price-change history.

**Zero-setup:** runs on SQLite out of the box — no database to install.

## Features

- **Scrapes** any Apple Refurbished page(s), any locale — Mac Studio, Mac mini,
  MacBook Pro… whatever URLs you configure.
- **Parses** Apple's embedded `application/ld+json` (with an HTML fallback):
  part number, title, price, RAM, storage, chip, CPU/GPU cores.
- **Diffs** against the previous run and records every *appeared*,
  *disappeared* and *price_changed* event.
- **Notifies** via [ntfy](https://ntfy.sh) — but only for products matching your
  filters (keywords, min RAM per category, optional price ceiling). Notifications
  are entirely optional.
- **Dashboard** (Flask): what's available now + recent events + scraper health.
- **SQLite by default**, MariaDB/MySQL optional.

## Quickstart

```bash
git clone https://github.com/lolfr/refurb_watcher.git
cd refurb_watcher
./scripts/install.sh           # venv + deps + config.toml
$EDITOR config.toml            # choose the URLs + filters you want
./venv/bin/python watcher.py   # first run (creates refurb.db)
./venv/bin/python web.py       # dashboard on http://127.0.0.1:5060
```

Then schedule `watcher.py` to run periodically — see
[`scripts/crontab.example`](scripts/crontab.example).

## Configuration

Everything lives in `config.toml` (copied from `config.toml.example`):

| Section | What |
|---|---|
| `[apple] urls` | The refurb pages to watch (any Apple locale). |
| `[database] backend` | `"sqlite"` (default) or `"mariadb"`. |
| `[ntfy]` | Push notifications — leave `url` empty to disable. |
| `[filters]` | Which *new* products trigger a notification (keywords + per-category rules). |
| `[dashboard]` | Flask host/port. |

## Notifications (optional)

Set `[ntfy] url` to an ntfy topic (e.g. `https://ntfy.sh/your-topic`) and install
the ntfy app on your phone. Add a `token` only if your topic requires auth. Leave
`url` empty and the scraper just records to the database silently.

## MariaDB instead of SQLite (optional)

```toml
[database]
backend  = "mariadb"
host     = "localhost"
user     = "refurb"
password = "…"
database = "refurb_watcher"
```

Then `pip install pymysql` and apply [`schema.sql`](schema.sql) once. The SQLite
backend creates its tables automatically, so `schema.sql` is only for MariaDB.

## How it works

`watcher.py` fetches each configured URL, `parser.py` extracts products
(primarily from the `ld+json` Product blocks Apple embeds, with an HTML
fallback), `db.py` diffs against the stored snapshot and records events, and
matching new products are pushed via ntfy. `web.py` serves a read-only dashboard
over the same database.

Apple changes its markup without notice, so the parser is deliberately defensive
(an explicit error beats silently-wrong data), and the watcher never purges the
snapshot when a fetch returns zero products (anti-scrape / transient errors).

## License

[AGPL-3.0](LICENSE) — © 2026 lolfr.
