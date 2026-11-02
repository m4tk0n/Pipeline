#!/usr/bin/env python3
"""Aktuální nabídka z letáku Tesco Supermarket Rajhrad (v2, ověřuje počet stránek).

Problém: API vrátilo pro slug "tesco-letak-RRRR-MM-DD" jiný (větší) leták než ten,
který ukazuje prodejna (34 místo 16 stran). Tento skript:
  1. přečte z viewera očekávaný počet stránek ("1 / 16"),
  2. z JS webu vytáhne GraphQL dotaz a seznam jeho proměnných,
  3. z __NEXT_DATA__ viewera vezme hodnoty pro tyto proměnné (typ letáku apod.),
  4. zkouší varianty volání API, dokud počet stran nesedí,
  5. vypíše diagnostiku a produkty (sonda).
Využívá pomocné funkce z itesco_parser.py.
"""
import json
import re
import sys
from urllib.parse import urljoin

import requests

import itesco_parser as ip

MAX_LIST = 11000          # znaků na seznam produktů (celý výstup má limit 20 000)
BASE_VARS = {"country", "slug"}
ENTITY_KEYS = {           # název proměnné (malými) -> klíče, které hledat v datech stránky
    "leafletid": ("id", "leafletId"),
    "id": ("id",),
    "leaflettype": ("type", "leafletType"),
    "type": ("type", "leafletType", "format"),
    "format": ("format", "type"),
    "storeformat": ("storeFormat", "format", "type"),
    "storeid": ("storeId", "store", "storeCode"),
    "store": ("store", "storeId"),
}


def cut(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def visible_text(html):
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def expected_pages(viewer_html):
    match = re.search(r"\b1\s*/\s*(\d{1,3})\b", visible_text(viewer_html))
    return int(match.group(1)) if match else None


def load_next_data(viewer_html):
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', viewer_html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def scalar_pool(next_data, slug):
    """Skalární hodnoty z dat stránky: nejdřív leták se shodným slugem, pak zbytek."""
    if not isinstance(next_data, dict):
        return [], []
    entities = [d for d in walk(next_data)
                if d.get("slug") == slug or ("validFrom" in d and "validTo" in d)
                or "leaflet" in str(d.get("__typename", "")).lower()]
    entities.sort(key=lambda d: 0 if d.get("slug") == slug else 1)
    pools = [e for e in entities]
    if isinstance(next_data.get("query"), dict):
        pools.append(next_data["query"])
    return entities, pools


def lookup(var_name, pools):
    keys = ENTITY_KEYS.get(var_name.lower(), ()) + (var_name,)
    for pool in pools:
        for key in keys:
            value = pool.get(key)
            if isinstance(value, (str, int, bool)) and str(value).strip() != "":
                return value
    return None


def declared_variables(query):
    match = re.search(r"query\s+%s\s*\(([^)]*)\)" % ip.QUERY_NAME, query)
    if not match:
        return []
    return re.findall(r"\$(\w+)\s*:\s*([\w\[\]!]+)", match.group(1))


def build_attempts(declared, pools, slug):
    base = {"country": ip.COUNTRY, "slug": slug}
    attempts = [("základní (country + slug)", dict(base))]
    extras = {}
    for name, _type in declared:
        if name not in BASE_VARS:
            value = lookup(name, pools)
            if value is not None:
                extras[name] = value
    if extras:
        attempts.append(("s hodnotami z __NEXT_DATA__", {**base, **extras}))
    for name, _type in declared:
        if name in BASE_VARS or name in extras:
            continue
        if "type" in name.lower() or "format" in name.lower():
            for guess in ("SM", "HM"):
                attempts.append((f"odhad {name}={guess}", {**base, **extras, name: guess}))
    return attempts


def call_api(session, document, variables):
    response = session.post(
        ip.API_URL,
        json={"query": document, "variables": variables},
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Origin": ip.BASE, "Referer": ip.BASE + "/"},
        timeout=45,
    )
    try:
        payload = response.json()
    except ValueError:
        return None, f"HTTP {response.status_code}: {response.text[:200]}"
    if payload.get("errors"):
        return None, "GraphQL: " + json.dumps(payload["errors"], ensure_ascii=False)[:300]
    leaflet = (payload.get("data") or {}).get("leafletBySlug")
    if not isinstance(leaflet, dict):
        return None, "odpověď bez leafletBySlug"
    return leaflet, None


def page_stats(leaflet):
    pages = [p for p in (leaflet.get("pages") or []) if isinstance(p, dict)]
    numbers = sorted({p.get("page") for p in pages if isinstance(p.get("page"), int)})
    return len(pages), (numbers[0] if numbers else None), (numbers[-1] if numbers else None)


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "cs-CZ,cs;q=0.9",
    })

    store_html = ip._get(session, ip.STORE_URL).text
    links = []
    for match in ip.FLYER_LINK_RE.finditer(store_html):
        if match.group(1) not in links:
            links.append(match.group(1))
    if not links:
        print("CHYBA: na stránce prodejny není žádný odkaz na leták.")
        return 2
    print("Letáky na stránce prodejny:", *links[:5], sep="\n  ")
    flyer_url = urljoin(ip.BASE, links[0]) + "/1"
    slug = ip.SLUG_RE.search(flyer_url).group(1)

    viewer_html = ip._get(session, flyer_url, headers={"Referer": ip.STORE_URL}).text
    expected = expected_pages(viewer_html)
    print(f"Viewer: {flyer_url}\nOčekávaný počet stránek (viewer): {expected}")

    next_data = load_next_data(viewer_html)
    entities, pools = scalar_pool(next_data, slug)
    print(f"__NEXT_DATA__: {'ano' if next_data else 'NE'}; letákových záznamů: {len(entities)}")
    for entity in entities[:3]:
        flat = {k: v for k, v in entity.items() if isinstance(v, (str, int, bool)) and len(str(v)) < 60}
        print("  záznam:", cut(json.dumps(flat, ensure_ascii=False), 300))

    scripts = []
    for path in ip.SCRIPT_RE.findall(viewer_html):
        if path not in scripts:
            scripts.append(path)
    scripts.sort(key=lambda p: (0 if "viewer" in p else 1 if "_app" in p else 2))
    js_texts, query, fragments = [], None, {}
    for path in scripts[:12]:
        try:
            js_texts.append(ip._get(session, urljoin(ip.BASE, path)).text)
        except requests.RequestException:
            continue
        query, fragments = ip.extract_documents(js_texts)
        if query and fragments:
            break
    if not query:
        print("CHYBA: v JS souborech nebyl nalezen dotaz", ip.QUERY_NAME)
        return 2
    document = ip.build_document(query, fragments)
    declared = declared_variables(query)
    print("Proměnné dotazu:", ", ".join(f"${n}: {t}" for n, t in declared) or "(nenalezeny)")

    best = None
    for label, variables in build_attempts(declared, pools, slug):
        try:
            leaflet, error = call_api(session, document, variables)
        except requests.RequestException as exc:
            leaflet, error = None, f"{type(exc).__name__}: {exc}"
        if error:
            print(f"[pokus] {label}: {error}")
            continue
        count, first, last = page_stats(leaflet)
        scalars = {k: v for k, v in leaflet.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80}
        match = expected is None or count == expected
        print(f"[pokus] {label}: stran {count} (č. {first}–{last}), shoda s viewerem: "
              f"{'ANO' if match else 'NE'}\n        {cut(json.dumps(scalars, ensure_ascii=False), 300)}")
        if best is None or (match and not best[3]):
            best = (label, variables, leaflet, match)
        if match:
            break

    if best is None:
        print("CHYBA: žádné volání API nevrátilo leták.")
        return 2
    label, variables, leaflet, match = best
    products, count = ip.extract_products(leaflet)

    print()
    print("=" * 60)
    print("Obchod:    Tesco Supermarket Rajhrad")
    print("Leták:    ", leaflet.get("promoP1Name") or leaflet.get("name"))
    print("Platnost: ", leaflet.get("validFrom"), "→", leaflet.get("validTo"))
    print(f"Stránek:   {count} (viewer: {expected}) | produktů: {len(products)}")
    print("Volání:   ", label, json.dumps(variables, ensure_ascii=False))
    if not match:
        print("VAROVÁNÍ: počet stran NESEDÍ s viewerem – pravděpodobně jiný leták (např. hypermarket).")
    print("=" * 60)

    used = shown = 0
    for item in sorted(products, key=lambda p: (p.get("page") or 0, p.get("title") or "")):
        price = item.get("price")
        line = "s.{:>2} | {} | {}".format(
            item.get("page") or "?", cut(item.get("title"), 50),
            price if price and price not in ("0", "0.0") else "cena?")
        if item.get("description"):
            line += " | " + cut(item["description"], 35)
        if used + len(line) > MAX_LIST:
            break
        print(line)
        used += len(line) + 1
        shown += 1
    if shown < len(products):
        print(f"… a dalších {len(products) - shown} produktů")

    if not products:
        return 2
    return 0 if match else 3


if __name__ == "__main__":
    sys.exit(main())
