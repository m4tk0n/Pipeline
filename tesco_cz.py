#!/usr/bin/env python3
"""Nabídka z aktuálního letáku Tesco Supermarket Rajhrad: jeden záznam na produkt.

Pole: id (Tesco), name, brand, amount, unit, url_image, url_product, valid_from, valid_to
(+ description, price, pages, amount_text, name_leaflet).

Postup:
  1. Z viewera zjistí očekávaný počet stran a z JS dotaz getLeafletPages.
  2. Zkouší volání API (leafletType/storeId z dat stránky), dokud počet stran nesedí.
  3. Z položek letáku složí záznamy (ID z odkazu /products/<ID>, platnost z letáku).
  4. Chybějící údaje (značka, obrázek, čistý obsah, plný název) doplní ze stránky produktu
     na nakup.itesco.cz; když to obchod blokuje, pokračuje jen s daty z letáku.
  5. Vypíše dávku produktů (OD, DAVKA) jako JSON řádky; další dávku zjistíš z patičky.
Využívá pomocné funkce z itesco_parser.py.
"""
import concurrent.futures as cf
import datetime as dt
import json
import re
import sys
import threading
import time
from html import unescape
from urllib.parse import urljoin

import requests

import itesco_parser as ip

SHOP_URL = "https://nakup.itesco.cz/shop/cs-CZ/products/{}"
# ---- NASTAVENÍ (stačí přepsat číslo v poli 2 na dashboardu a spustit znovu) ----
OD = 0                     # od kolikátého produktu (podle pořadí v letáku) vypisovat
DAVKA = 40                 # kolik produktů vypsat najednou (výstup má limit 20 000 znaků)
# --------------------------------------------------------------------------------
ENRICH_SECONDS = 60        # na doplňování ze stránek produktů (běh má limit 120 s)
MAX_LIST = 16500           # znaků na seznam produktů ve výstupu
FIELDS = ("id", "name", "brand", "amount", "unit", "url_image", "url_product", "valid_from", "valid_to")
BASE_VARS = {"country", "slug"}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "cs-CZ,cs;q=0.9",
}
ENTITY_KEYS = {
    "leafletid": ("id", "leafletId"),
    "id": ("id",),
    "leaflettype": ("type", "leafletType"),
    "type": ("type", "leafletType", "format"),
    "format": ("format", "type"),
    "storeformat": ("storeFormat", "format", "type"),
    "storeid": ("storeId", "store", "storeCode"),
    "store": ("store", "storeId"),
}
UNITS = r"(kg|g|ml|cl|dl|l|ks)"


def cut(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


# ----------------------------------------------------------------- výběr správného letáku

def visible_text(page_html):
    page_html = re.sub(r"<!--.*?-->", "", page_html, flags=re.S)
    page_html = re.sub(r"<(script|style).*?</\1>", " ", page_html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", page_html)


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
    if not isinstance(next_data, dict):
        return [], []
    entities = [d for d in walk(next_data)
                if d.get("slug") == slug or ("validFrom" in d and "validTo" in d)
                or "leaflet" in str(d.get("__typename", "")).lower()]
    entities.sort(key=lambda d: 0 if d.get("slug") == slug else 1)
    pools = list(entities)
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
        ip.API_URL, json={"query": document, "variables": variables},
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


def find_leaflet(session):
    """Vrátí (leaflet, popis volání, proměnné, shoda, očekávané stránky) nebo None."""
    store_html = ip._get(session, ip.STORE_URL).text
    links = []
    for match in ip.FLYER_LINK_RE.finditer(store_html):
        if match.group(1) not in links:
            links.append(match.group(1))
    if not links:
        print("CHYBA: na stránce prodejny není žádný odkaz na leták.")
        return None
    print("Letáky na stránce prodejny:", *links[:5], sep="\n  ")
    flyer_url = urljoin(ip.BASE, links[0]) + "/1"
    slug = ip.SLUG_RE.search(flyer_url).group(1)

    viewer_html = ip._get(session, flyer_url, headers={"Referer": ip.STORE_URL}).text
    expected = expected_pages(viewer_html)
    print(f"Viewer: {flyer_url}\nOčekávaný počet stránek (viewer): {expected}")

    next_data = load_next_data(viewer_html)
    entities, pools = scalar_pool(next_data, slug)
    print(f"__NEXT_DATA__: {'ano' if next_data else 'NE'}; letákových záznamů: {len(entities)}")

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
        return None
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
        scalars = {k: v for k, v in leaflet.items()
                   if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80}
        match = expected is None or count == expected
        print(f"[pokus] {label}: stran {count} (č. {first}–{last}), shoda s viewerem: "
              f"{'ANO' if match else 'NE'}\n        {cut(json.dumps(scalars, ensure_ascii=False), 300)}")
        if best is None or (match and not best[3]):
            best = (leaflet, label, variables, match, expected)
        if match:
            break
    if best is None:
        print("CHYBA: žádné volání API nevrátilo leták.")
    return best


# ----------------------------------------------------------------- záznamy o produktech

def local_date(iso):
    if not iso:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        try:
            from zoneinfo import ZoneInfo
            moment = moment.astimezone(ZoneInfo("Europe/Prague"))
        except Exception:  # noqa: BLE001
            pass
        return moment.date().isoformat()
    except ValueError:
        return str(iso)[:10]


def flatten(node, prefix="", depth=0, out=None):
    out = {} if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                if depth < 3:
                    flatten(value, path, depth + 1, out)
            else:
                out[path] = value
    elif isinstance(node, list):
        for index, value in enumerate(node[:3]):
            flatten(value, f"{prefix}[{index}]", depth + 1, out)
    return out


def pick(flat, key_re, ok=lambda v: True):
    for path, value in flat.items():
        leaf = re.sub(r"\[\d+\]", "", path).split(".")[-1]
        if (re.search(key_re, leaf, re.I) and isinstance(value, (str, int, float))
                and str(value).strip() and ok(value)):
            return value
    return None


def parse_amount(text):
    """Vrátí (množství, jednotka, původní text) z názvu nebo čistého obsahu."""
    text = str(text or "")
    multi = re.search(r"(\d+)\s*x\s*(\d+(?:[.,]\d+)?)\s*%s\b" % UNITS, text, re.I)
    if multi:
        total = int(multi.group(1)) * float(multi.group(2).replace(",", "."))
        return round(total, 3), multi.group(3).lower(), multi.group(0)
    single = re.search(r"(\d+(?:[.,]\d+)?)\s*%s\b" % UNITS, text, re.I)
    if single:
        return float(single.group(1).replace(",", ".")), single.group(2).lower(), single.group(0)
    tail = re.search(r"\b(kg|ks)\s*\.?$", text.strip(), re.I)
    if tail:
        return None, tail.group(1).lower(), tail.group(0)
    return None, None, None


def extract_items(leaflet):
    for page in leaflet.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for position in page.get("positions") or []:
            if not isinstance(position, dict):
                continue
            for item in position.get("products") or []:
                if isinstance(item, dict):
                    inner = item.get("product") if isinstance(item.get("product"), dict) else {}
                    yield item, inner, position, page.get("page")


def build_records(leaflet):
    valid_from, valid_to = local_date(leaflet.get("validFrom")), local_date(leaflet.get("validTo"))
    records, by_key = [], {}
    first_flat = None
    for item, inner, position, number in extract_items(leaflet):
        flat = flatten(item)
        first_flat = first_flat or flat
        url = str(item.get("addToShareURL") or position.get("hyperlinkUrl") or "")
        found = re.search(r"/products/(\d+)", url)
        pid = found.group(1) if found else pick(
            flat, r"^(tpnc|tpnb|productid|gtin|sku|id)$", lambda v: str(v).isdigit() and len(str(v)) >= 6)
        name = str(inner.get("productName") or item.get("promoOfferName") or "").strip()
        if not name:
            continue
        key = str(pid) if pid else (name, number)
        if key in by_key:
            if number not in by_key[key]["pages"]:
                by_key[key]["pages"].append(number)
            continue
        price = ip._find_price(item, inner)
        amount, unit, amount_text = parse_amount(name)
        image = pick(flat, r"image|img|photo|picture|thumb", lambda v: str(v).startswith(("http", "//")))
        record = {
            "id": str(pid) if pid else None,
            "name": name,
            "brand": pick(flat, r"brand") or None,
            "amount": amount,
            "unit": unit,
            "url_image": image,
            "url_product": SHOP_URL.format(pid) if pid else (url or None),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "description": str(item.get("promoOfferName") or "").strip() or None,
            "price": None if str(price).strip() in ("", "0", "0.0") else price,
            "pages": [number],
            "amount_text": amount_text,
            "name_leaflet": name,
        }
        by_key[key] = record
        records.append(record)
    return records, first_flat or {}


# ----------------------------------------------------------------- doplnění ze stránky produktu

_local = threading.local()


def shop_session():
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _local.session = session
    return session


def parse_shop(page_html):
    metas = {}
    for tag in re.findall(r"<meta\b[^>]*>", page_html, re.I):
        key = re.search(r'(?:property|name)\s*=\s*"([^"]+)"', tag, re.I)
        value = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I)
        if key and value:
            metas.setdefault(key.group(1).lower(), unescape(value.group(1)))

    product = None
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        product = next((d for d in walk(data) if str(d.get("@type", "")).lower() == "product"), None)
        if product:
            break
    product = product or {}

    brand = product.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    if not brand:
        found = re.search(r'"brand(?:Name)?"\s*:\s*"([^"]{1,60})"', page_html)
        brand = found.group(1) if found else None

    image = product.get("image")
    image = image[0] if isinstance(image, list) and image else image
    image = image or metas.get("og:image")
    image = str(image).split("?")[0] if image else None

    text = visible_text(page_html)
    content = re.search(r"Čistý obsah\s*(\d+(?:[.,]\d+)?)\s*%s\b" % UNITS, text, re.I)
    amount = unit = None
    if content:
        amount, unit = float(content.group(1).replace(",", ".")), content.group(2).lower()

    return {
        "name": (product.get("name") or metas.get("og:title") or "").strip() or None,
        "brand": str(brand).strip() if brand else None,
        "url_image": image,
        "amount": amount,
        "unit": unit,
        "amount_text": content.group(0).replace("Čistý obsah", "").strip() if content else None,
    }


def enrich(records):
    ids = sorted({r["id"] for r in records if r["id"]})
    stats = {"ok": 0, "fail": 0, "skipped": 0, "errors": {}}
    results = {}
    stop = threading.Event()
    deadline = time.monotonic() + ENRICH_SECONDS

    def work(pid):
        if stop.is_set() or time.monotonic() > deadline:
            return pid, None, None
        try:
            response = shop_session().get(SHOP_URL.format(pid), timeout=12)
        except requests.RequestException as exc:
            return pid, None, type(exc).__name__
        if response.status_code != 200:
            return pid, None, f"HTTP {response.status_code}"
        return pid, parse_shop(response.text), None

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for future in cf.as_completed([pool.submit(work, pid) for pid in ids]):
            pid, info, error = future.result()
            if info:
                stats["ok"] += 1
                results[pid] = info
            elif error is None:
                stats["skipped"] += 1
            else:
                stats["fail"] += 1
                stats["errors"][error] = stats["errors"].get(error, 0) + 1
                if stats["ok"] == 0 and stats["fail"] >= 8:
                    stop.set()   # obchod požadavky blokuje, nemá smysl pokračovat

    for record in records:
        info = results.get(record["id"])
        if not info:
            continue
        record["name"] = info["name"] or record["name"]
        record["brand"] = info["brand"] or record["brand"]
        record["url_image"] = info["url_image"] or record["url_image"]
        if info["amount"] is not None or info["unit"]:
            record["amount"], record["unit"] = info["amount"], info["unit"]
            record["amount_text"] = info["amount_text"] or record["amount_text"]
    return stats


# ----------------------------------------------------------------- hlavní běh

def first_page(record):
    pages = [p for p in record["pages"] if isinstance(p, int)]
    return min(pages) if pages else 0


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    best = find_leaflet(session)
    if best is None:
        return 2
    leaflet, label, variables, match, expected = best
    records, first_flat = build_records(leaflet)
    records.sort(key=lambda r: (first_page(r), r["name"]))
    count = page_stats(leaflet)[0]
    total = len(records)

    start = max(0, int(OD))
    chunk = records[start:start + max(1, int(DAVKA))]
    stats = enrich(chunk)          # doplňuje se jen vypisovaná dávka (rychlé)

    print()
    print("=" * 60)
    print(f"Leták: id {leaflet.get('id')}, typ {leaflet.get('type')} | platnost "
          f"{local_date(leaflet.get('validFrom'))} → {local_date(leaflet.get('validTo'))}")
    print(f"Stránek: {count} (viewer: {expected}) | produktů celkem: {total}")
    print(f"Volání: {label} {json.dumps(variables, ensure_ascii=False)}")
    print(f"Obchod (dávka): doplněno {stats['ok']}, chyb {stats['fail']}, přeskočeno {stats['skipped']}"
          + (f" {json.dumps(stats['errors'], ensure_ascii=False)}" if stats["errors"] else ""))
    if not match:
        print("VAROVÁNÍ: počet stran NESEDÍ s viewerem – pravděpodobně jiný leták (např. hypermarket).")
    if start == 0:
        print("Pole položky letáku:", cut(", ".join(first_flat) or "-", 300))
    print("=" * 60)

    used = shown = 0
    for record in chunk:
        line = json.dumps({k: record[k] for k in FIELDS}, ensure_ascii=False, separators=(",", ":"))
        if used + len(line) > MAX_LIST:
            break
        print(line)
        used += len(line) + 1
        shown += 1

    print("-" * 60)
    end = start + shown
    if end < total:
        print(f"Zobrazeno {start + 1}–{end} z {total}. Další dávka: nastav OD = {end}")
    else:
        print(f"Zobrazeno {start + 1}–{end} z {total}. Hotovo, to je celý leták.")

    if not records:
        return 2
    return 0 if match else 3


if __name__ == "__main__":
    sys.exit(main())
