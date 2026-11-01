#!/usr/bin/env python3
"""Tesco Rajhrad flyer parser. Writes itesco_rajhrad_data.json."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

OUTPUT = Path("itesco_rajhrad_data.json")
CATALOG_URL = "https://itesco.cz/akce-a-letaky/letaky/"


def parse_flyer() -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://publitas.com/",
    }
    session = requests.Session()
    session.headers.update(headers)

    flyer_url = None
    error_msg = None
    products = []
    title = "Tesco Supermarket Rajhrad - Aktuální leták"
    total_pages = 0

    try:
        response = session.get(CATALOG_URL, timeout=20)
        response.raise_for_status()
        matches = re.findall(
            r'(https?://[^\s<>"\']+\.publitas\.com[^\s<>"\']*)',
            response.text,
        )
        if matches:
            flyer_url = matches[0].rstrip(".,)")
        else:
            error_msg = "V katalogu Tesco nebyla nalezena URL Publitas."
    except requests.RequestException as exc:
        error_msg = f"Chyba při stahování katalogu Tesco: {exc}"

    if flyer_url:
        try:
            base_url = flyer_url.split("/page/")[0].rstrip("/")
            api_response = session.get(f"{base_url}/config.json", timeout=20)
            api_response.raise_for_status()
            config = api_response.json()
            publication = config.get("publication", {})
            title = publication.get("title") or title
            pages = publication.get("pages") or []
            total_pages = len(pages)

            for page in pages:
                page_number = page.get("number", 1)
                for hotspot in page.get("hotspots", []):
                    product_title = hotspot.get("title") or hotspot.get("name")
                    if not product_title:
                        continue
                    products.append(
                        {
                            "title": str(product_title).strip(),
                            "description": str(hotspot.get("description") or "").strip(),
                            "price": str(hotspot.get("price") or ""),
                            "page": page_number,
                            "product_url": hotspot.get("url") or hotspot.get("webshop_url"),
                        }
                    )
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            error_msg = f"Chyba při čtení Publitas API: {exc}"

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
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Parser dokončen. Stránek: {total_pages}; produktů: {len(products)}")
    if error_msg:
        print(f"Chyba parseru: {error_msg}")
    return result


if __name__ == "__main__":
    data = parse_flyer()
    # Prázdný výsledek je pro tento konkrétní test neúspěch, nikoli technický crash.
    if not data.get("products"):
        sys.exit(2)
