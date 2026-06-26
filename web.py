#!/usr/bin/env python3
"""refurb_watcher — minimal Flask dashboard.

Routes:
- /          : products currently listed + recent events
- /history   : all events (appeared / disappeared / price_changed)
- /health    : scraper health (recent polls)
- /api/...   : JSON versions

Binds to 127.0.0.1 by default; put a reverse proxy in front (and add auth there)
if you expose it. Use a WSGI server (gunicorn/uwsgi) for production.
"""

from __future__ import annotations

from datetime import datetime

import tomli
from flask import Flask, jsonify, render_template, request
from flask_caching import Cache

import db as dbm


# ---------- Config ----------

with open("config.toml", "rb") as f:
    CFG = tomli.load(f)


app = Flask(__name__)
cache = Cache(
    app,
    config={
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": int(CFG["dashboard"].get("cache_seconds", 30)),
    },
)


# ---------- DB queries (SQL lives in db.py) ----------

def fetch_current() -> list[dict]:
    with dbm.connect(CFG["database"]) as db:
        return dbm.fetch_current(db)


def fetch_recent_events(limit: int = 100) -> list[dict]:
    with dbm.connect(CFG["database"]) as db:
        return dbm.fetch_recent_events(db, limit)


def fetch_health() -> dict:
    with dbm.connect(CFG["database"]) as db:
        return dbm.fetch_health(db)


# ---------- HTML routes ----------

RAM_FILTER_OPTIONS = [0, 24, 32, 48, 64, 96, 128]
CATEGORY_FILTER_OPTIONS = ["all", "Mac Studio", "MacBook Pro", "Mac mini", "iMac"]


@app.route("/")
def index():
    # Default filter: >= 96 GB RAM, all categories.
    try:
        min_ram = int(request.args.get("min_ram", 96))
    except ValueError:
        min_ram = 96
    if min_ram not in RAM_FILTER_OPTIONS:
        min_ram = 96
    cat_filter = request.args.get("cat", "all")
    if cat_filter not in CATEGORY_FILTER_OPTIONS:
        cat_filter = "all"

    products_all = fetch_current()

    def _keep(p: dict) -> bool:
        if min_ram > 0 and (p.get("ram_gb") is None or p["ram_gb"] < min_ram):
            return False
        if cat_filter != "all" and p.get("category") != cat_filter:
            return False
        return True

    products = [p for p in products_all if _keep(p)]

    events = fetch_recent_events(20)
    health = fetch_health()
    last_ok = health["last_ok"]
    stale_minutes = None
    if last_ok:
        stale_minutes = int((datetime.now() - last_ok).total_seconds() / 60)

    by_category: dict[str, list] = {}
    for p in products:
        by_category.setdefault(p["category"], []).append(p)

    return render_template(
        "index.html",
        by_category=by_category,
        events=events,
        stale_minutes=stale_minutes,
        last_ok=last_ok,
        total_products=len(products),
        total_in_db=len(products_all),
        min_ram=min_ram,
        cat_filter=cat_filter,
        ram_options=RAM_FILTER_OPTIONS,
        category_options=CATEGORY_FILTER_OPTIONS,
        now=datetime.now(),
    )


@app.route("/history")
@cache.cached()
def history():
    limit = int(request.args.get("limit", 200))
    events = fetch_recent_events(limit)
    return render_template("history.html", events=events, now=datetime.now())


@app.route("/health")
def health():
    h = fetch_health()
    return render_template("health.html", health=h, now=datetime.now())


# ---------- JSON API ----------

@app.route("/api/current")
def api_current():
    products = fetch_current()
    for p in products:
        p["first_seen"] = p["first_seen"].isoformat() if p["first_seen"] else None
        p["last_seen"] = p["last_seen"].isoformat() if p["last_seen"] else None
    return jsonify(products)


@app.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 100))
    events = fetch_recent_events(limit)
    for e in events:
        e["seen_at"] = e["seen_at"].isoformat() if e["seen_at"] else None
    return jsonify(events)


# ---------- Jinja filters ----------

@app.template_filter("euro")
def euro_filter(cents):
    if cents is None:
        return "—"
    return f"{cents / 100:,.2f} €".replace(",", " ").replace(".", ",")


@app.template_filter("dt")
def dt_filter(d):
    if d is None:
        return "—"
    return d.strftime("%d/%m %H:%M")


@app.template_filter("relative")
def relative_filter(d):
    if d is None:
        return "—"
    delta = datetime.now() - d
    if delta.days > 0:
        return f"il y a {delta.days} j"
    hours = delta.seconds // 3600
    if hours > 0:
        return f"il y a {hours} h"
    mins = delta.seconds // 60
    return f"il y a {mins} min"


if __name__ == "__main__":
    app.run(
        host=CFG["dashboard"]["host"],
        port=int(CFG["dashboard"]["port"]),
        debug=False,
    )
