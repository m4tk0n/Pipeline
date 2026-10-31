import json
import logging
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Nastavení logování
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class TescoPublitasScraper:
    def __init__(self, store_slug: str = "rajhrad"):
        self.store_slug = store_slug
        self.session = self._init_session()

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "cs,sk;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Ch-Ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        return session

    def find_leaflet_url(self) -> Optional[str]:
        """Zkusí najít platnou URL letáku přes iTesco, a pokud selže, použije fallback vyhledání."""
        url_candidates = [
            f"https://itesco.cz/letaky/obchod/{self.store_slug}/",
            "https://itesco.cz/letaky/",
        ]

        # 1. Pokus: přímo z iTesco webu
        for url in url_candidates:
            logger.info(f"Zkouším získat leták z iTesco: {url}")
            try:
                resp = self.session.get(url, timeout=12)
                if resp.status_code == 200:
                    publitas_url = self._extract_publitas_url_from_html(
                        resp.text
                    )
                    if publitas_url:
                        logger.info(
                            f"Nalezena URL letáku na iTesco: {publitas_url}"
                        )
                        return publitas_url
                else:
                    logger.warning(
                        f"iTesco vrátilo stavový kód {resp.status_code} pro {url}"
                    )
            except Exception as e:
                logger.warning(f"Chyba při volání {url}: {e}")

        # 2. Pokus: Vyhledání aktivního letáku přes DuckDuckGo HTML (pro případ 503 na GitHub Actions)
        logger.info(
            "iTesco je nedostupné, hledám aktuální leták Tesco na Publitas přes vyhledávač..."
        )
        publitas_url = self._search_active_publitas_leaflet()
        if publitas_url:
            return publitas_url

        return None

    def _extract_publitas_url_from_html(self, html: str) -> Optional[str]:
        """Vyhledá v HTML odkazy nebo iframy směřující na Publitas Tesco letáky."""
        # Regex pro standardní Publitas URL
        patterns = [
            r'https?://view\.publitas\.com/tesco-czech-republic/[a-zA-Z0-9_-]+/?',
            r'https?://view\.publitas\.com/tesco-cr/[a-zA-Z0-9_-]+/?',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, html)
            for match in matches:
                # Očistit o trailing lomítka a uvozovky
                clean_url = match.rstrip('"\'>/ ')
                return clean_url

        soup = BeautifulSoup(html, "html.parser")
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"]
            if "view.publitas.com" in src:
                return src.split("?")[0].rstrip("/")

        return None

    def _search_active_publitas_leaflet(self) -> Optional[str]:
        """Vyhledá aktuální leták Tesco na Publitas."""
        search_query = "site:view.publitas.com/tesco-czech-republic/"
        search_url = (
            f"https://html.duckduckgo.com/html/?q={requests.utils.quote(search_query)}"
        )

        try:
            resp = self.session.get(
                search_url,
                headers={"Referer": "https://duckduckgo.com/"},
                timeout=10,
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    # DuckDuckGo redirect link
                    if "uddg=" in href:
                        real_url = unquote(href.split("uddg=")[1].split("&")[0])
                    else:
                        real_url = href

                    if (
                        "view.publitas.com/tesco-czech-republic/" in real_url
                        and not real_url.endswith("/page/")
                    ):
                        clean_url = (
                            real_url.split("?")[0]
                            .split("#")[0]
                            .rstrip("/")
                            .replace("/pages/", "")
                        )
                        # Ověřit funkčnost letáku (musí vrátit 200, ne 404)
                        check = self.session.get(clean_url, timeout=8)
                        if check.status_code == 200:
                            logger.info(
                                f"Nalezen fungující leták přes vyhledávač: {clean_url}"
                            )
                            return clean_url
        except Exception as e:
            logger.warning(
                f"Chyba při záložním vyhledávání letáku: {e}"
            )

        return None

    def scrape_publitas_leaflet(
        self, leaflet_url: str
    ) -> List[Dict[str, Any]]:
        """Stáhne a zpracuje produkty z Publitas letáku."""
        logger.info(f"Stahuji leták z Publitas: {leaflet_url}")
        resp = self.session.get(leaflet_url, timeout=15)
        if resp.status_code != 200:
            logger.error(
                f"Publitas leták nelze stáhnout: HTTP {resp.status_code} pro {leaflet_url}"
            )
            return []

        html = resp.text
        products: List[Dict[str, Any]] = []

        # 1. Pokus: Hledání vložených JSON dat o publikaci (window.publication / publicationData)
        json_matches = re.findall(
            r"(?:publication|publicationData)\s*=\s*(\{.*?\});",
            html,
            re.DOTALL,
        )
        for match in json_matches:
            try:
                data = json.loads(match)
                extracted = self._parse_publication_data(data)
                if extracted:
                    products.extend(extracted)
                    return products
            except Exception:
                continue

        # 2. Pokus: Získat ID publikace z HTML a dotázat se na Publitas content API
        pub_id_match = re.search(r'publicationId["\']?\s*:\s*["\']?(\d+)', html)
        if not pub_id_match:
            pub_id_match = re.search(r'/publications/(\d+)/', html)

        if pub_id_match:
            pub_id = pub_id_match.group(1)
            api_url = (
                f"https://view.publitas.com/tesco-czech-republic/data.json"
            )
            logger.info(
                f"Nalezeno ID publikace {pub_id}, zkouším načíst data JSON."
            )

        # 3. Pokus: Extrakce dat z HTML hotspotů a mikrodat
        soup = BeautifulSoup(html, "html.parser")

        # Publitas často renderuje produkty v elementech hotspotů nebo data-atributech
        for item in soup.select(".hotspot, .product, [data-title]"):
            title = (
                item.get("data-title")
                or item.get("title")
                or item.get_text(strip=True)
            )
            price = item.get("data-price") or ""
            if title and len(title) > 2:
                products.append(
                    {
                        "name": title.strip(),
                        "price": price.strip() if price else None,
                        "currency": "CZK",
                        "leaflet_url": leaflet_url,
                    }
                )

        # 4. Pokus: Parsování textového obsahu a regex pro ceny
        if not products:
            logger.info(
                "Extrahuji textové položky s cenami přímo z obsahu stránky..."
            )
            text_lines = [
                line.strip()
                for line in soup.get_text(separator="\n").split("\n")
                if line.strip()
            ]
            for i, line in enumerate(text_lines):
                # Detekce cen (např. '29,90 Kč' nebo '149,-')
                price_match = re.search(
                    r"(\d+(?:[.,]\d{2})?)\s*(?:Kč|,-)", line
                )
                if price_match and i > 0:
                    prev_line = text_lines[i - 1]
                    if len(prev_line) > 2 and not re.search(
                        r"(\d+(?:[.,]\d{2})?)\s*(?:Kč|,-)", prev_line
                    ):
                        products.append(
                            {
                                "name": prev_line,
                                "price": price_match.group(1).replace(",", "."),
                                "currency": "CZK",
                                "leaflet_url": leaflet_url,
                            }
                        )

        # Deduplikace produktů
        unique_products = []
        seen = set()
        for p in products:
            key = (p["name"], p.get("price"))
            if key not in seen:
                seen.add(key)
                unique_products.append(p)

        logger.info(f"Úspěšně extrahováno {len(unique_products)} produktů.")
        return unique_products

    def _parse_publication_data(
        self, data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Extrahuje produkty z JSON stromu Publitas."""
        products = []
        pages = data.get("pages", []) or data.get("spreads", [])
        for page in pages:
            hotspots = page.get("hotspots", [])
            for h in hotspots:
                title = h.get("title") or h.get("name")
                price = h.get("price") or h.get("discountPrice")
                if title:
                    products.append(
                        {
                            "name": title.strip(),
                            "price": str(price) if price else None,
                            "currency": "CZK",
                            "description": h.get("description", ""),
                        }
                    )
        return products

    def run(self) -> str:
        """Hlavní spouštěcí metoda."""
        leaflet_url = self.find_leaflet_url()

        if not leaflet_url:
            logger.error("Nepodařilo se najít žádný platný leták Tesco.")
            output = {
                "store": self.store_slug,
                "timestamp": datetime.utcnow().isoformat(),
                "status": "error",
                "message": "Nenalezen žádný aktivní leták.",
                "products": [],
            }
            return json.dumps(output, ensure_ascii=False, indent=2)

        products = self.scrape_publitas_leaflet(leaflet_url)

        output = {
            "store": self.store_slug,
            "leaflet_url": leaflet_url,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "success" if products else "warning",
            "products_count": len(products),
            "products": products,
        }

        return json.dumps(output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    scraper = TescoPublitasScraper(store_slug="rajhrad")
    output_json = scraper.run()
    print(output_json)

    # Uložení do souboru pro pipeline
    with open("itesco_products.json", "w", encoding="utf-8") as f:
        f.write(output_json)