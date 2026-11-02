#!/usr/bin/env python3
# Cesta: Pipeline/itesco_parser.py
"""Tesco Rajhrad flyer parser (GraphQL). Writes itesco_rajhrad_data.json.

Postup: stránka prodejny -> odkaz na leták -> JS soubory vieweru -> z nich se
vytáhne skutečný GraphQL dotaz (getLeafletPages + fragmenty) -> POST na API.
Dotaz se neodhaduje, bere se z aktuálního JS webu, takže přežije změny polí.
"""
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
API_URL = "https://api.prod.retail.tesco.com/marketing/leaflets-be/graphql"
COUNTRY = "cz"
QUERY_NAME = "getLeafletPages"

FLYER_LINK_RE = re.compile(
    r'(?:https?://www\.itesco\.cz)?(/akcni-nabidky/letaky-a-katalogy/[^"\'\s<>]*?/tesco-letak-[0-9-]+)/\d+'
)
SLUG_RE = re.compile(r"(tesco-letak-\d{4}-\d{2}-\d{2})")
SCRIPT_RE = re.compile(r'(/customer-leaflets-fe-assets/_next/static/[^"\'\s<>]+\.js)')
FRAGMENT_RE = re.compile(r"\bfragment\s+(\w+)\s+on\s+(\w+)")
SPREAD_RE = re.compile(r"\.\.\.\s*(\w+)")
PAGES_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=30, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


# ---------- vytažení GraphQL dokumentů z JS ----------

def _literal_around(js: str, index: int) -> str | None:
    """Vrátí dekódovaný obsah JS řetězce ("...") obsahujícího pozici index."""
    start = js.rfind('"', 0, index)
    if start < 0:
        return None
    i = index
    while i < len(js):
        if js[i] == "\\":
            i += 2
            continue
        if js[i] == '"':
            break
        i += 1
    raw = js[start + 1:i]
    try:
        return json.loads('"' + raw + '"')
    except ValueError:
        return None


def _balanced(text: str, start: int) -> str:
    """Od start (první znak definice) po uzavírací } odpovídající první {."""
    depth = 0
    seen = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if ch == "{":
            depth += 1
            seen = True
        elif ch == "}":
            depth -= 1
            if seen and depth == 0:
                return text[start:pos + 1]
    return text[start:]


def extract_documents(js_texts: list[str]) -> tuple[str | None, dict[str, str]]:
    query = None
    fragments: dict[str, str] = {}
    for js in js_texts:
        if query is None:
            idx = js.find(f"query {QUERY_NAME}")
            if idx >= 0:
                literal = _literal_around(js, idx)
                if literal:
                    pos = literal.find(f"query {QUERY_NAME}")
                    query = _balanced(literal, pos)
        for match in FRAGMENT_RE.finditer(js):
            name = match.group(1)
            if name in fragments:
                continue
            literal = _literal_around(js, match.start())
            if not literal:
                continue
            pos = literal.find(match.group(0))
            if pos >= 0:
                fragments[name] = _balanced(literal, pos)
    return query, fragments


def build_document(query: str, fragments: dict[str, str]) -> str:
    needed: list[str] = []
    queue = SPREAD_RE.findall(query)
    while queue:
        name = queue.pop()
        if name in needed or name not in fragments:
            continue
        needed.append(name)
        queue.extend(SPREAD_RE.findall(fragments[name]))
    return "\n".join([query] + [fragments[n] for n in needed])


# ---------- zpracování odpovědi ----------

def _find_price(*nodes, depth: int = 2) -> str:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if "price" in key.lower() and isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        if depth > 0:
            for value in node.values():
                if isinstance(value, dict):
                    found = _find_price(value, depth=depth - 1)
                    if found:
                        return found
    return ""


def extract_products(leaflet: dict) -> tuple[list[dict], int]:
    products: list[dict] = []
    seen = set()
    pages = leaflet.get("pages") or []
    for page in pages:
        if not isinstance(page, dict):
            continue
        number = page.get("page")
        for position in page.get("positions") or []:
            if not isinstance(position, dict):
                continue
            for item in position.get("products") or []:
                if not isinstance(item, dict):
                    continue
                inner = item.get("product") if isinstance(item.get("product"), dict) else {}
                title = str(inner.get("productName") or item.get("promoOfferName") or "").strip()
                if not title:
                    continue
                offer = str(item.get("promoOfferName") or "").strip()
                price = _find_price(item, inner)
                key = (title, number, price)
                if key in seen:
                    continue
                seen.add(key)
                products.append({
                    "title": title,
                    "description": offer if offer and offer != title else "",
                    "price": price,
                    "page": number,
                    "product_url": item.get("addToShareURL") or position.get("hyperlinkUrl"),
                })
    return products, len(pages)


def parse_flyer() -> dict:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "cs-CZ,cs;q=0.9",
    })

    flyer_url = None
    error_msg = None
    products: list[dict] = []
    title = "Tesco Supermarket Rajhrad - Aktuální leták"
    total_pages = 0
    valid_from = valid_to = None

    try:
        store_html = _get(session, STORE_URL).text
        link = FLYER_LINK_RE.search(store_html)
        if not link:
            raise RuntimeError("Na stránce prodejny nebyl nalezen odkaz na aktuální leták.")
        flyer_url = urljoin(BASE, link.group(1)) + "/1"
        slug = SLUG_RE.search(flyer_url).group(1)  # type: ignore[union-attr]

        viewer_html = _get(session, flyer_url, headers={"Referer": STORE_URL}).text
        pages_match = PAGES_RE.search(viewer_html)
        if pages_match:
            total_pages = int(pages_match.group(2))

        script_paths: list[str] = []
        for path in SCRIPT_RE.findall(viewer_html):
            if path not in script_paths:
                script_paths.append(path)
        # viewer a _app chunk mají přednost
        script_paths.sort(key=lambda p: (0 if "viewer" in p else 1 if "_app" in p else 2))
        print(f"Leták: {flyer_url}; skriptů: {len(script_paths)}")

        js_texts: list[str] = []
        query = None
        fragments: dict[str, str] = {}
        for path in script_paths[:12]:
            try:
                js_texts.append(_get(session, urljoin(BASE, path)).text)
            except requests.RequestException:
                continue
            query, fragments = extract_documents(js_texts)
            if query and fragments:
                break
        if not query:
            raise RuntimeError(f"V JS souborech nebyl nalezen dotaz {QUERY_NAME}.")

        document = build_document(query, fragments)
        print(f"GraphQL dotaz: {len(document)} znaků, fragmenty: {sorted(fragments)}")

        response = session.post(
            API_URL,
            json={"query": document, "variables": {"country": COUNTRY, "slug": slug}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": BASE,
                "Referer": BASE + "/",
            },
            timeout=45,
        )
        print(f"API HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"API nevrátilo JSON (HTTP {response.status_code}): {response.text[:300]}")
        if payload.get("errors"):
            raise RuntimeError("GraphQL chyby: " + json.dumps(payload["errors"], ensure_ascii=False)[:600])

        leaflet = (payload.get("data") or {}).get("leafletBySlug")
        if not isinstance(leaflet, dict):
            raise RuntimeError("Odpověď neobsahuje leafletBySlug: " + json.dumps(payload, ensure_ascii=False)[:300])

        title = leaflet.get("promoP1Name") or leaflet.get("name") or title
        valid_from, valid_to = leaflet.get("validFrom"), leaflet.get("validTo")
        products, page_count = extract_products(leaflet)
        total_pages = page_count or total_pages

        if products:
            sample = products[0]
            print("Ukázka produktu:", json.dumps(sample, ensure_ascii=False))
            if not any(p["price"] for p in products):
                error_msg = ("Produkty nalezeny, ale bez ceny; klíče v odpovědi: "
                             + ", ".join(sorted((leaflet.get("pages") or [{}])[0].keys()))[:300])
        else:
            first_page = (leaflet.get("pages") or [{}])[0]
            error_msg = "Leták načten, ale bez produktů. Klíče stránky: " + ", ".join(sorted(first_page.keys()))
    except (requests.RequestException, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

    result = {
        "meta": {
            "store": "Tesco Supermarket Rajhrad",
            "flyer_title": title,
            "flyer_url": flyer_url,
            "total_pages": total_pages,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "scraped_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "error": error_msg,
        },
        "products": products,
    }

    # Zapisuj jen při nalezených produktech; poslední dobrá data zůstanou.
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
    if not data.get("products") or data["meta"].get("error"):
        sys.exit(2)
