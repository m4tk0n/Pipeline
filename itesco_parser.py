#!/usr/bin/env python3
import json
import requests
import re
from datetime import datetime, timezone

def parse_itesco_flyer():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://publitas.com/"
    }
    
    session = requests.Session()
    session.headers.update(headers)
    
    # 1. Zkusíme najít aktuální Publitas ID pro Tesco ČR / Rajhrad z katalogu nebo vyhledáním
    # Publitas obvykle hostuje letáky na adrese typu https://view.publitas.com/...
    catalog_url = "https://itesco.cz/akce-a-letaky/letaky/"
    print(🔍 Hledám odkaz na leták v Tesco katalogu...")
    
    flyer_url = None
    try:
        resp = session.get(catalog_url, timeout=10)
        if resp.status_code == 200:
            # Hledáme odkaz obsahující publitas
            matches = re.findall(r'(https?://[^\s<>"\']+\.publitas\.com[^\s<>"\']*)', resp.text)
            if matches:
                flyer_url = matches[0]
                print(f"✅ Nalezen Publitas URL: {flyer_url}")
    except Exception as e:
        print(f"⚠️ Chyba při stahování katalogu Tesco: {e}")

    # Fallback, pokud by se přímý odkaz nenašel
    if not flyer_url:
        # Zkusíme standardní cestu nebo napřímo vrátíme strukturovanou chybu
        output = {
            "meta": {
                "store": "Tesco Supermarket Rajhrad",
                "flyer_title": None,
                "flyer_url": None,
                "total_pages": 0,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "error": "Nepodařilo se detekovat Publitas odkaz z Tesco stránek."
            },
            "products": []
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 2. Zkusíme z Publitas URL vytáhnout API data
    # Publitas končí obvykle na /page/1 nebo slug. Stahujeme config.json nebo pages.json
    try:
        # Příklad struktury Publitas: https://view.publitas.com/tesco-ceska-republika/aktualni-letak/page/1
        # API je na https://view.publitas.com/tesco-ceska-republika/aktualni-slug/config.json
        base_api_url = flyer_url.split('/page/')[0].rstrip('/') + '/config.json'
        print(f"🔄 Stahuji Publitas konfigurační API: {base_api_url}")
        
        api_resp = session.get(base_api_url, timeout=10)
        if api_resp.status_code == 200:
            config = api_resp.json()
            publication = config.get("publication", {})
            title = publication.get("title", "Tesco Leták")
            pages = publication.get("pages", [])
            
            products = []
            for page in pages:
                page_num = page.get("number", 1)
                for hotspot in page.get("hotspots", []):
                    p_title = hotspot.get("title") or hotspot.get("name")
                    if p_title:
                        products.append({
                            "title": p_title.strip(),
                            "description": hotspot.get("description", "").strip(),
                            "price": str(hotspot.get("price", "")),
                            "page": page_num,
                            "product_url": hotspot.get("url") or hotspot.get("webshop_url")
                        })
            
            result = {
                "meta": {
                    "store": "Tesco Supermarket Rajhrad",
                    "flyer_title": title,
                    "flyer_url": flyer_url,
                    "total_pages": len(pages),
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
                "products": products
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

    except Exception as e:
        print(f"⚠️ Přímé čtení Publitas API selhalo: {e}")

    # Pokud selhalo i API, vrátíme prázdný stav s popisem
    fallback_output = {
        "meta": {
            "store": "Tesco Supermarket Rajhrad",
            "flyer_title": flyer_url,
            "flyer_url": flyer_url,
            "total_pages": 0,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "error": "Publitas blokuje přímý JSON scraping nebo se zmenila struktura endpointu."
        },
        "products": []
    }
    print(json.dumps(fallback_output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parse_itesco_flyer()
