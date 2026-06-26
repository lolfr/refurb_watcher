#!/usr/bin/env python3
"""
# version: 0.1.0
refurb-watcher : scraper + diff + notification

Lancé par cron toutes les 10-30 min.
Cycle :
1. Pour chaque URL surveillée : fetch HTML, parse produits, log poll
2. Calcule diff vs snapshot DB (apparu / disparu / prix changé)
3. Met à jour le snapshot
4. Notifie via ntfy les apparitions qui matchent les filtres
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import tomli

from db import (
    connect,
    current_snapshot,
    delete_disappeared,
    log_event,
    log_poll,
    upsert_product,
)
from parser import Product, parse


# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watcher")


# ---------- Config ----------

def load_config(path: str = "config.toml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        log.error("config.toml introuvable. Copier config.toml.example.")
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomli.load(f)


# ---------- Fetch ----------

def fetch(url: str, user_agent: str, timeout: int) -> tuple[int, str, int]:
    """Retourne (http_status, html, duration_ms)."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        duration_ms = int((time.monotonic() - t0) * 1000)
        return resp.status_code, resp.text, duration_ms
    except requests.RequestException as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        log.error("Fetch failed for %s : %s", url, e)
        return 0, "", duration_ms


# ---------- Filtres notification ----------

def should_notify(product: Product, filters_cfg: dict) -> bool:
    """Décide si un produit nouveau mérite une notif."""
    keywords = filters_cfg.get("keywords", [])
    if keywords:
        title_lc = product.title.lower()
        if not any(kw.lower() in title_lc for kw in keywords):
            return False

    must_match_list = filters_cfg.get("must_match", [])
    if not must_match_list:
        return True

    # Si une règle catégorielle existe pour la catégorie du produit, elle doit passer
    relevant_rules = [r for r in must_match_list if r.get("category") == product.category]
    if not relevant_rules:
        # pas de règle pour cette catégorie => on laisse passer (filtré par keywords déjà)
        return True

    for rule in relevant_rules:
        min_ram = rule.get("min_ram_gb")
        if min_ram is not None:
            if product.ram_gb is None or product.ram_gb < min_ram:
                return False
        max_price = rule.get("max_price_eur")
        if max_price is not None:
            if product.price_cents > max_price * 100:
                return False
    return True


# ---------- Notification ntfy ----------

def _ascii_safe(s: str) -> str:
    """ntfy headers : ASCII direct si possible, sinon RFC 2047 UTF-8 en
    base64 sur une seule ligne. Préserve accents français + ligatures.
    """
    if not s:
        return s
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        import base64
        b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


def _safe_url(s: str) -> str:
    """Percent-encode les caractères non-ASCII d'une URL pour les headers."""
    if not s:
        return s
    from urllib.parse import quote
    return quote(s, safe=":/?#[]@!$&'()*+,;=%~")


def notify_ntfy(cfg: dict, product: Product, event_type: str = "appeared") -> None:
    """Envoie une notif ntfy."""
    url = (cfg or {}).get("url") or ""
    if not url:
        return  # notifications disabled when no ntfy url is configured
    headers = {
        "Title": _ascii_safe(f"Refurb : {product.title[:80]}"),
        "Priority": cfg.get("priority", "default"),
        "Tags": "computer",
        "Click": _safe_url(product.url),
    }
    token = cfg.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"

    price_eur = product.price_cents / 100
    savings_str = ""
    if product.savings_cents:
        savings_str = f" (économie : {product.savings_cents / 100:.0f} €)"

    body = (
        f"{product.title}\n"
        f"Prix : {price_eur:.2f} €{savings_str}\n"
        f"Part : {product.part_number}\n"
        f"{product.url}"
    )

    try:
        resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=10)
        if resp.status_code >= 300:
            log.warning("ntfy push échoué (%d) : %s", resp.status_code, resp.text[:200])
        else:
            log.info("ntfy push OK : %s", product.part_number)
    except requests.RequestException as e:
        log.error("ntfy push exception : %s", e)


# ---------- Cycle principal ----------

def run_once(cfg: dict) -> int:
    """Un cycle complet. Retourne le nombre de notifications envoyées."""
    now = datetime.now()
    notif_count = 0

    # Collecte tous les produits vus dans ce cycle (toutes URLs confondues)
    all_products: dict[str, Product] = {}

    for url in cfg["apple"]["urls"]:
        status, html, duration = fetch(
            url,
            cfg["apple"]["user_agent"],
            int(cfg["apple"]["timeout_seconds"]),
        )

        if status != 200:
            with connect(cfg["database"]) as conn:
                log_poll(conn, now, url, status, None, f"HTTP {status}", duration)
            continue

        try:
            products = parse(html, url)
        except Exception as e:
            log.exception("Parsing failed for %s", url)
            with connect(cfg["database"]) as conn:
                log_poll(conn, now, url, status, None, f"parse error: {e}", duration)
            continue

        with connect(cfg["database"]) as conn:
            log_poll(conn, now, url, status, len(products), None, duration)

        for p in products:
            # Si même part_number trouvé sur 2 URLs (rare), on garde la 1re
            if p.part_number not in all_products:
                all_products[p.part_number] = p

    log.info("Total produits vus dans ce cycle : %d", len(all_products))

    if not all_products:
        # Si on a 0 produit alors qu'on en avait avant, suspect (anti-détection ou erreur)
        # On NE PURGE PAS le snapshot dans ce cas.
        log.warning("0 produit parsé ce cycle, snapshot non touché")
        return 0

    # Diff & MAJ DB
    with connect(cfg["database"]) as conn:
        snapshot = current_snapshot(conn)
        snapshot_keys = set(snapshot.keys())
        current_keys = set(all_products.keys())

        appeared = current_keys - snapshot_keys
        disappeared_keys = snapshot_keys - current_keys
        still_present = current_keys & snapshot_keys

        # Apparitions
        for pn in appeared:
            p = all_products[pn]
            upsert_product(conn, p.to_dict(), now, is_new=True)
            notify = should_notify(p, cfg["filters"])
            log_event(
                conn,
                pn,
                "appeared",
                now,
                p.price_cents,
                p.title,
                notify,
            )
            log.info(
                "NEW: %s | %s | %.2f € | notify=%s",
                pn,
                p.title[:60],
                p.price_cents / 100,
                notify,
            )
            if notify:
                notify_ntfy(cfg.get("ntfy", {}), p, "appeared")
                notif_count += 1

        # Mises à jour (prix changé éventuellement)
        for pn in still_present:
            p = all_products[pn]
            old = snapshot[pn]
            upsert_product(conn, p.to_dict(), now, is_new=False)
            if old["price_cents"] != p.price_cents:
                log_event(
                    conn,
                    pn,
                    "price_changed",
                    now,
                    p.price_cents,
                    p.title,
                    False,
                )
                log.info(
                    "PRICE: %s : %.2f € -> %.2f €",
                    pn,
                    old["price_cents"] / 100,
                    p.price_cents / 100,
                )

        # Disparitions
        for old in delete_disappeared(conn, current_keys):
            log_event(
                conn,
                old["part_number"],
                "disappeared",
                now,
                old["price_cents"],
                old["title"],
                False,
            )
            log.info("GONE: %s | %s", old["part_number"], old["title"][:60])

    return notif_count


def main() -> int:
    cfg = load_config("config.toml")
    notifs = run_once(cfg)
    log.info("Cycle terminé. %d notifications envoyées.", notifs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
