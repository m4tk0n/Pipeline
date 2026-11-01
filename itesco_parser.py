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

import requests

OUTPUT = Path(__file__).resolve().parent / "itesco_rajhrad_data.json"
CATALOG_URL = "https://itesco.cz/akce-a-letaky/letaky/"
PUBLITAS_RE = re.compile(r'https?:(?:\\?/){2}[A-Za-z0-9.-]*publitas\.com[^\s<>"\'\\]*')
# Hostitelé, které jsou pravděpodobně assety/CDN, ne vlastní leták.
IGNORED_HOST_PARTS = ("cdn.", "assets.", "static.", "view.publitas.com/embed")


def _find_flyer_url(html: str) -> str | None:
    seen: list[str] = []
    for raw in PUBLITAS_RE.findall(html):
        url = raw.replace("\\/", "/").rstrip(".,)")
        if url not in seen:
            seen.append(url)
    for url in seen:
        if not any(part in url for part in IGNORED_HOST_PARTS):
            return url
    return seen[0] if seen else None


def parse_flyer() -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9",
    }
    session = requests.Session()
    session.headers.update(headers)

    flyer_url = None
    error_msg = None
    products: list[dict] = []
    title = "Tesco Supermarket Rajhrad - Aktuální leták"
    total_pages = 0

    for attempt in range(3):
        try:
            response = session.get(CATALOG_URL, timeout=20)
            response.raise_for_status()
            flyer_url = _find_flyer_url(response.text)
            error_msg = None if flyer_url else "V katalogu Tesco nebyla nalezena URL Publitas."
            break
        except requests.RequestException as exc:
            error_msg = f"Chyba při stahování katalogu Tesco: {exc}"
            if attempt < 2:
                time.sleep(3 * (attempt + 1))

    if flyer_url:
        try:
            base_url = flyer_url.split("/page/")[0].rstrip("/")
            api_response = session.get(
                f"{base_url}/config.json",
                timeout=20,
                headers={"Accept": "application/json", "Referer": base_url + "/"},
            )
            api_response.raise_for_status()
            config = api_response.json()
            publication = config.get("publication", {})
            title = publication.get("title") or title
            pages = publication.get("pages") or []
            total_pages = len(pages)

            for page in pages:
                page_number = page.get("number", 1)
                for hotspot in page.get("hotspots") or []:
                    product_title = hotspot.get("title") or hotspot.get("name")
                    if not product_title:
                        continue
                    products.append(
                        {
                            "title": str(product_title).strip(),
                            "description": str(hotspot.get("description") or "").strip(),
                            "price": str(hotspot.get("price") or "").strip(),
                            "page": page_number,
                            "product_url": hotspot.get("url") or hotspot.get("webshop_url"),
                        }
                    )
            if not products and not error_msg:
                error_msg = "Leták byl načten, ale neobsahuje žádné produkty (hotspots)."
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
            error_msg = f"Chyba při čtení Publitas API: {type(exc).__name__}: {exc}"

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

    # Při neúspěchu nepřepisuj poslední dobrá data prázdným výsledkem.
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
