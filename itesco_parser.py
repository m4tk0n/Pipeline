#!/usr/bin/env python3
# Cesta: Pipeline/itesco_parser.py
"""Tesco Rajhrad flyer parser. Writes itesco_rajhrad_data.json."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

OUTPUT = Path(__file__).resolve().parent / "itesco_rajhrad_data.json"
BASE = "https://www.itesco.cz"
STORE_URL = f"{BASE}/akcni-nabidky/letaky-a-katalogy/tesco-supermarket-rajhrad"
# Odkaz na aktuální leták, např. .../supermarkety/tesco-supermarket-rajhrad/tesco-letak-2026-10-29/1
FLYER_LINK_RE = re.compile(
    r'(?:https?://www\.itesco\.cz)?(/akcni-nabidky/letaky-a-katalogy/[^"\'\s<>]*?/tesco-letak-[0-9-]+)/\d+'
)
PAGES_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
PUBLITAS_RE = re.compile(r'https?:(?:\\?/){2}[A-Za-z0-9.-]*publitas\.com[^\s<>"\'\\]*')
HINT_RE = re.compile(r'["\'(=\s]((?:https?:)?//[^"\'\s)<>]+|/[A-Za-z0-9_\-./]+\.(?:json|js))', re.I)


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=20, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _diagnostics(html: str) -> str:
    """Stručný přehled odkazů z viewer stránky, aby agent věděl, odkud se berou data."""
    hints: list[str] = []
    for match in HINT_RE.findall(html):
        if any(k in match.lower() for k in ("json", "api", "leaflet", "letak", "publitas", "flyer", ".js")):
            if match not in hints:
                hints.append(match)
    return "; ".join(hints[:15])[:1500]


def parse_flyer() -> dict:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9",
    })

    flyer_url = None
    error_msg = None
    products: list[dict] = []
    title = "Tesco Supermarket Rajhrad - Aktuální leták"
    total_pages = 0

    try:
        store_html = _get(session, STORE_URL).text
        link = FLYER_LINK_RE.search(store_html)
        if link:
            flyer_url = urljoin(BASE, link.group(1)) + "/1"
        else:
            error_msg = "Na stránce prodejny nebyl nalezen odkaz na aktuální leták."
    except requests.RequestException as exc:
        error_msg = f"Chyba při stahování stránky prodejny: {exc}"

    if flyer_url:
        try:
            viewer_html = _get(session, flyer_url, headers={"Referer": STORE_URL}).text
            pages = PAGES_RE.search(viewer_html)
            if pages:
                total_pages = int(pages.group(2))
            print(f"Leták: {flyer_url}; stránek: {total_pages}")

            # Pokud viewer používá Publitas, zkus config.json s hotspoty.
            publitas = PUBLITAS_RE.search(viewer_html)
            if publitas:
                base_url = publitas.group(0).replace("\\/", "/").split("/page/")[0].rstrip("/")
                config = _get(session, f"{base_url}/config.json",
                              headers={"Accept": "application/json", "Referer": base_url + "/"}).json()
                publication = config.get("publication", {})
                title = publication.get("title") or title
                for page in publication.get("pages") or []:
                    for hotspot in page.get("hotspots") or []:
                        name = hotspot.get("title") or hotspot.get("name")
                        if name:
                            products.append({
                                "title": str(name).strip(),
                                "description": str(hotspot.get("description") or "").strip(),
                                "price": str(hotspot.get("price") or "").strip(),
                                "page": page.get("number", 1),
                                "product_url": hotspot.get("url") or hotspot.get("webshop_url"),
                            })

            if not products and not error_msg:
                error_msg = ("Leták nalezen, ale viewer je JavaScript aplikace a produkty nejsou v HTML. "
                             "Zdroje dat: " + (_diagnostics(viewer_html) or "žádné nalezeny"))
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
            error_msg = f"Chyba při čtení letáku: {type(exc).__name__}: {exc}"

    result = {
        "meta": {
            "store": "Tesco Supermarket Rajhrad",
            "flyer_title": title,
            "flyer_url": flyer_url,
            "total_pages": total_pages,
            "scraped_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "error": error_msg,
        },
        "products": products,
    }

    # Při neúspěchu nepřepisuj poslední dobrá data.
    if products:
        tmp = OUTPUT.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OUTPUT)

    print(f"Parser dokončen. Stránek: {total_pages}; produktů: {len(products)}")
    if error_msg:
        print(f"Chyba parseru: {error_msg}")
    return result


if __name__ == "__main__":
    data = parse_flyer()
    if not data.get("products"):
        sys.exit(2)
