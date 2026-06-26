"""
# version: 0.1.0
refurb-watcher : extraction des produits depuis le HTML Apple Refurb FR

Apple Refurb FR rend le HTML côté serveur. Les produits sont dans un bloc
JSON embarqué (visible via View Source). Stratégie en 3 couches :

1. Chercher le JSON dans un <script> (le plus stable)
2. Si non trouvé : parser les <li> de la liste produits (fallback HTML)
3. Si échec total : remonter l'exception, log poll en erreur

Ce parser est volontairement défensif : Apple change sa structure sans
préavis. Mieux vaut une erreur explicite qu'une donnée silencieusement fausse.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup


log = logging.getLogger(__name__)


@dataclass
class Product:
    part_number: str
    category: str          # "Mac Studio" / "Mac mini" / "MacBook Pro"
    title: str
    price_cents: int
    list_price_cents: Optional[int]
    savings_cents: Optional[int]
    currency: str          # "EUR"
    url: str
    ram_gb: Optional[int]
    storage_gb: Optional[int]
    chip: Optional[str]    # "M3 Ultra", "M4 Max", etc.
    cpu_cores: Optional[int]
    gpu_cores: Optional[int]
    raw_specs: str

    def to_dict(self) -> dict:
        return self.__dict__


# Catégorisation à partir du titre
CATEGORY_PATTERNS = [
    (re.compile(r"Mac Studio", re.IGNORECASE), "Mac Studio"),
    (re.compile(r"Mac mini", re.IGNORECASE), "Mac mini"),
    (re.compile(r"MacBook Pro", re.IGNORECASE), "MacBook Pro"),
    (re.compile(r"MacBook Air", re.IGNORECASE), "MacBook Air"),
    (re.compile(r"iMac", re.IGNORECASE), "iMac"),
]


def categorize(title: str) -> str:
    for pattern, label in CATEGORY_PATTERNS:
        if pattern.search(title):
            return label
    return "Other"


# ---------- Parsing prix ----------

PRICE_RE = re.compile(r"([\d\s\u00a0]+)[,.]\s*(\d{2})\s*€")


def parse_price_to_cents(text: str) -> Optional[int]:
    """Parse '4 199,00 €' -> 419900 (centimes). Tolère les espaces insécables."""
    if not text:
        return None
    cleaned = text.replace("\u202f", " ").replace("\u00a0", " ").strip()
    m = PRICE_RE.search(cleaned)
    if not m:
        # tentative simple sans cents : '4 199 €'
        m2 = re.search(r"([\d\s]+)\s*€", cleaned)
        if m2:
            digits = re.sub(r"\D", "", m2.group(1))
            if digits:
                return int(digits) * 100
        return None
    euros = int(re.sub(r"\D", "", m.group(1)))
    cents = int(m.group(2))
    return euros * 100 + cents


# ---------- Extraction specs depuis le titre ou la description ----------

RAM_RE = re.compile(r"(\d+)\s*Go\s*(?:de\s*)?(?:m[ée]moire|RAM|unifi)", re.IGNORECASE)
# Stockage : Apple écrit "SSD 4 To" / "4 To SSD" / "SSD 512 Go" / "512 Go SSD"
# / "SSD de 256 Go" / "SSD de 1 To" (format ld+json description).
STORAGE_RE = re.compile(
    r"(?:SSD\s*(?:de\s*)?(\d+)\s*(Go|To)|(\d+)\s*(Go|To)\s*SSD)",
    re.IGNORECASE,
)
CHIP_RE = re.compile(r"\b(M[1-9](?:\s+(?:Pro|Max|Ultra))?)\b")
# Apple écrit "32 cœurs CPU" ou "Puce 32 cœurs CPU 80 cœurs GPU"
# Note : c'est cœurs (c-œ-u-r-s) ou coeurs (c-o-e-u-r-s), pas cœeurs
CPU_CORES_RE = re.compile(r"(\d+)\s*(?:c[œ]urs?|coeurs?)\s*CPU", re.IGNORECASE)
GPU_CORES_RE = re.compile(r"(\d+)\s*(?:c[œ]urs?|coeurs?)\s*GPU", re.IGNORECASE)


def extract_specs(text: str) -> dict:
    """Extrait RAM, stockage, chip, cores depuis le titre + description."""
    specs = {
        "ram_gb": None,
        "storage_gb": None,
        "chip": None,
        "cpu_cores": None,
        "gpu_cores": None,
    }
    if not text:
        return specs

    # Apple colle l'année de commercialisation à la RAM dans la description
    # ld+json : "...commercialisé en octobre 2024" + "16 Go de mémoire unifiée"
    # rendu sans espace => "...octobre 202416 Go...". RAM_RE avalait alors
    # "202416". On réinsère une frontière entre l'année (20xx) et le chiffre suivant.
    text = re.sub(r"(20\d{2})(\d)", r"\1 \2", text)

    m = RAM_RE.search(text)
    if m:
        val = int(m.group(1))
        # garde-fou : la mémoire unifiée Apple plausible reste <= 1024 Go ;
        # au-delà = concaténation parasite résiduelle -> on préfère null à du faux.
        specs["ram_gb"] = val if val <= 1024 else None

    m = STORAGE_RE.search(text)
    if m:
        # group(1)+group(2) ou group(3)+group(4) selon quelle variante a matché
        val = int(m.group(1) or m.group(3))
        unit = (m.group(2) or m.group(4) or "").lower()
        if unit == "to":
            val *= 1000
        specs["storage_gb"] = val

    m = CHIP_RE.search(text)
    if m:
        specs["chip"] = m.group(1).replace("  ", " ").strip()

    m = CPU_CORES_RE.search(text)
    if m:
        specs["cpu_cores"] = int(m.group(1))

    m = GPU_CORES_RE.search(text)
    if m:
        specs["gpu_cores"] = int(m.group(1))

    return specs


# ---------- Stratégie 1 : ld+json schema.org Product (Apple Refurb FR) ----------
#
# Apple expose un <script type="application/ld+json"> par produit, format
# schema.org/Product. Exemple :
# {
#   "@type": "Product",
#   "name": "MacBook Pro 14 pouces reconditionné…",
#   "url":  "https://www.apple.com/fr/shop/product/fde04f/a/…",
#   "offers": [{ "@type": "Offer", "price": 1899.0, "priceCurrency": "EUR",
#                "sku": "FDE04F/A", … }],
#   "description": "… 16 Go de mémoire unifiée … SSD de 1 To …",
#   "color": "Noir sidéral"
# }
# Pas de list_price barré dans le JSON — on laissera null.

MAX_REASONABLE_PRICE_CENTS = 50_000 * 100  # 50 000 € — anti overflow + anti footer-junk


def parse_embedded_json(html: str, base_url: str) -> list[Product]:
    """Extrait les produits depuis les <script type="application/ld+json"> de
    type Product. Robuste à l'ordre, dédoublonne par part_number."""
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []
    seen_parts: set[str] = set()

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or ""
        if not text or "Product" not in text:
            continue
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        # Un script peut contenir 1 Product ou une liste
        candidates = obj if isinstance(obj, list) else [obj]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            if c.get("@type") != "Product":
                continue
            p = _product_from_ldjson(c, base_url)
            if p and p.part_number not in seen_parts:
                seen_parts.add(p.part_number)
                products.append(p)
    return products


def _normalize_whitespace(s: str) -> str:
    """Apple injecte des espaces insécables (U+00A0) un peu partout dans
    les titres + descriptions ld+json. Les normaliser en espaces ASCII pour
    que les regex categorize/RAM/STORAGE/CHIP matchent."""
    if not s:
        return s
    return re.sub(r"\s+", " ", s.replace(" ", " ").replace(" ", " "))


def _product_from_ldjson(obj: dict, base_url: str) -> Optional[Product]:
    """Construit un Product depuis un objet schema.org/Product."""
    try:
        title = _normalize_whitespace((obj.get("name") or "").strip())
        url = obj.get("url") or obj.get("mainEntityOfPage") or ""

        # offers peut être dict OU list
        offers = obj.get("offers") or []
        if isinstance(offers, dict):
            offers = [offers]
        if not offers:
            return None
        offer = offers[0]
        price_raw = offer.get("price")
        if price_raw is None:
            return None
        # price est typiquement un float ; tolère str numérique
        try:
            price_eur = float(price_raw)
        except (TypeError, ValueError):
            return None
        price_cents = int(round(price_eur * 100))
        if price_cents <= 0 or price_cents > MAX_REASONABLE_PRICE_CENTS:
            log.warning("Prix hors borne (%d cents) skip: %s", price_cents, title[:60])
            return None
        currency = offer.get("priceCurrency") or "EUR"

        # sku au format "FDE04F/A" — c'est le part_number canonique
        sku = (offer.get("sku") or "").strip()
        # fallback : extraire du URL : /fr/shop/product/fde04f/a/...
        if not sku:
            m = re.search(r"/shop/product/([a-z0-9]+)/([a-z])/", url, re.IGNORECASE)
            if m:
                sku = f"{m.group(1).upper()}/{m.group(2).upper()}"
        if not sku:
            return None

        # description + color = source specs (normalize whitespace pour les regex)
        description = _normalize_whitespace(obj.get("description") or "")
        color = _normalize_whitespace(obj.get("color") or "")
        text_for_specs = f"{title} {description} {color}"
        specs = extract_specs(text_for_specs)

        return Product(
            part_number=sku,
            category=categorize(title),
            title=title,
            price_cents=price_cents,
            list_price_cents=None,  # pas exposé dans ld+json Apple
            savings_cents=None,
            currency=currency,
            url=url or base_url,
            ram_gb=specs["ram_gb"],
            storage_gb=specs["storage_gb"],
            chip=specs["chip"],
            cpu_cores=specs["cpu_cores"],
            gpu_cores=specs["gpu_cores"],
            raw_specs=text_for_specs.strip()[:1000],
        )
    except Exception as e:
        log.warning("Erreur parsing ld+json Product: %s", e)
        return None


def _walk_json(node, base_url: str) -> list[Product]:
    """Parcourt récursivement un objet JSON pour y trouver des produits."""
    found: list[Product] = []
    if isinstance(node, dict):
        # Est-ce qu'on est sur un objet produit ?
        if "partNumber" in node and ("currentPrice" in node or "price" in node):
            p = _product_from_json_obj(node, base_url)
            if p:
                found.append(p)
        for v in node.values():
            found.extend(_walk_json(v, base_url))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_json(item, base_url))
    return found


def _product_from_json_obj(obj: dict, base_url: str) -> Optional[Product]:
    """Construit un Product depuis un objet JSON Apple."""
    try:
        part_number = obj.get("partNumber") or obj.get("part_number")
        if not part_number:
            return None
        title = obj.get("title") or obj.get("name") or ""

        # Prix : Apple expose plusieurs formats possibles
        price_obj = obj.get("currentPrice") or obj.get("price") or {}
        if isinstance(price_obj, dict):
            price_text = price_obj.get("raw_amount") or price_obj.get("amount") or ""
            price_cents = parse_price_to_cents(str(price_text))
            currency = price_obj.get("currencyCode", "EUR")
        else:
            price_cents = parse_price_to_cents(str(price_obj))
            currency = "EUR"

        list_price_obj = obj.get("regularPrice") or obj.get("listPrice") or {}
        if isinstance(list_price_obj, dict):
            list_price = parse_price_to_cents(
                str(list_price_obj.get("raw_amount") or list_price_obj.get("amount") or "")
            )
        else:
            list_price = parse_price_to_cents(str(list_price_obj)) if list_price_obj else None

        savings = (list_price - price_cents) if (list_price and price_cents) else None

        url = obj.get("url", "")
        if url and not url.startswith("http"):
            url = "https://www.apple.com" + url

        title_and_specs = title + " " + (obj.get("specs") or obj.get("description") or "")
        specs = extract_specs(title_and_specs)

        if price_cents is None:
            return None

        return Product(
            part_number=part_number,
            category=categorize(title),
            title=title.strip(),
            price_cents=price_cents,
            list_price_cents=list_price,
            savings_cents=savings,
            currency=currency,
            url=url or base_url,
            ram_gb=specs["ram_gb"],
            storage_gb=specs["storage_gb"],
            chip=specs["chip"],
            cpu_cores=specs["cpu_cores"],
            gpu_cores=specs["gpu_cores"],
            raw_specs=title_and_specs.strip()[:1000],
        )
    except Exception as e:
        log.warning("Erreur parsing produit JSON: %s", e)
        return None


# ---------- Stratégie 2 : fallback HTML ----------

def parse_html_fallback(html: str, base_url: str) -> list[Product]:
    """Fallback : parse les éléments DOM directement.
    Moins fiable, mais utile si la structure JSON change.
    """
    soup = BeautifulSoup(html, "lxml")
    products: list[Product] = []

    # Apple utilise des classes type 'rf-refurb-category-grid-no-js-tile' ou similaire
    # On cherche tous les containers qui ont à la fois un titre et un prix
    tiles = soup.find_all(
        lambda tag: tag.name in ("li", "div", "article")
        and tag.find(string=re.compile(r"€"))
        and tag.find(string=re.compile(r"(Mac|MacBook|iMac)", re.IGNORECASE))
    )

    seen_titles = set()
    for tile in tiles:
        title_el = tile.find(["h3", "h2", "h4"]) or tile.find(
            class_=re.compile(r"title", re.IGNORECASE)
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)

        price_text = ""
        for s in tile.stripped_strings:
            if "€" in s:
                price_text = s
                break
        price_cents = parse_price_to_cents(price_text)
        if price_cents is None:
            continue
        # Anti pollution footer : reject les prix anormalement gros
        if price_cents > MAX_REASONABLE_PRICE_CENTS:
            log.warning("HTML fallback skip prix hors borne %d cents (%s)",
                        price_cents, title[:60])
            continue

        link = tile.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = "https://www.apple.com" + url

        # Le part number est typiquement dans l'URL : .../FCYT3FN/A
        part_number_match = re.search(r"/([A-Z0-9]{5,}/?[A-Z]?)\b", url)
        part_number = part_number_match.group(1) if part_number_match else f"unknown-{hash(url) & 0xffff:x}"

        specs = extract_specs(title)
        products.append(
            Product(
                part_number=part_number,
                category=categorize(title),
                title=title,
                price_cents=price_cents,
                list_price_cents=None,
                savings_cents=None,
                currency="EUR",
                url=url or base_url,
                ram_gb=specs["ram_gb"],
                storage_gb=specs["storage_gb"],
                chip=specs["chip"],
                cpu_cores=specs["cpu_cores"],
                gpu_cores=specs["gpu_cores"],
                raw_specs=title[:1000],
            )
        )

    return products


# ---------- Point d'entrée ----------

def parse(html: str, base_url: str) -> list[Product]:
    """Tente JSON d'abord, puis fallback HTML."""
    products = parse_embedded_json(html, base_url)
    if products:
        log.info("Parsé %d produits via JSON embarqué", len(products))
        return products
    log.warning("JSON embarqué vide ou introuvable, fallback HTML")
    products = parse_html_fallback(html, base_url)
    log.info("Parsé %d produits via fallback HTML", len(products))
    return products
