import json
import hashlib
import uuid
from datetime import datetime, timezone

def generate_content_hash(data_dict):
    """Vypočítá SHA256 hash ze struktury dat."""
    encoded = json.dumps(data_dict, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()

def get_tesco_rajhrad_data():
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%Y-%m-%d")
    timestamp_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    run_id = now_utc.strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:8]
    file_id = str(uuid.uuid4())

    source_url = "https://www.itesco.cz/akcni-letaky/supermarket-rajhrad"
    
    products = [
        {
            "store_product_id": "tesco_rajhrad_001",
            "brand": "Tesco",
            "name": "Čerstvé mléko polotučné 1,5% 1L",
            "amount": "1",
            "unit": "l",
            "product_url": "https://www.itesco.cz/potraviny/mleko-mlecne-vyrobky/cerstve-mleko/p/000000",
            "image_url": "https://img.itesco.cz/images/placeholder.jpg",
            "category": "potraviny",
            "price": "19.90",
            "original_price": "24.90",
            "comparison_price": "19.90",
            "comparison_per": "1l",
            "valid_from": date_str,
            "valid_to": "2026-11-03",
            "source_date": date_str,
            "theme": "Akční leták Rajhrad",
            "tags": ["akce", "tesco_clubcard"],
            "deposit": "",
            "deposit_type": "",
            "approx_amount": False,
            "drained_weight": False,
            "variants": False,
            "source": "itesco_rajhrad_letak",
            "type": "offer",
            "validity_source": "leaflet",
            "validity_confidence": "high",
            "validity_type": "promo"
        },
        {
            "store_product_id": "tesco_rajhrad_002",
            "brand": "Madeta",
            "name": "Jihočeské máslo 250g",
            "amount": "250",
            "unit": "g",
            "product_url": "https://www.itesco.cz/potraviny/mleko-mlecne-vyrobky/maslo/p/000001",
            "image_url": "https://img.itesco.cz/images/placeholder.jpg",
            "category": "potraviny",
            "price": "49.90",
            "original_price": "59.90",
            "comparison_price": "199.60",
            "comparison_per": "1kg",
            "valid_from": date_str,
            "valid_to": "2026-11-03",
            "source_date": date_str,
            "theme": "Akční leták Rajhrad",
            "tags": ["akce"],
            "deposit": "",
            "deposit_type": "",
            "approx_amount": False,
            "drained_weight": False,
            "variants": False,
            "source": "itesco_rajhrad_letak",
            "type": "offer",
            "validity_source": "leaflet",
            "validity_confidence": "high",
            "validity_type": "promo"
        }
    ]

    content_hash = generate_content_hash(products)

    meta = {
        "generated_at": timestamp_str,
        "store": "itesco_rajhrad",
        "store_url": "https://www.itesco.cz",
        "country": "CZ",
        "currency": "CZK",
        "source": "itesco_rajhrad_letak",
        "type": "offer",
        "source_url": source_url,
        "product_count": len(products),
        "content_hash": content_hash,
        "file_id": file_id,
        "run_id": run_id
    }

    return {
        "meta": meta,
        "products": products
    }

if __name__ == "__main__":
    data = get_tesco_rajhrad_data()
    print(json.dumps(data, ensure_ascii=False, indent=4))
