#!/usr/bin/env python3
"""Aktuální nabídka z letáku Tesco Supermarket Rajhrad.

Využívá hotový parser itesco_parser.py (leták -> GraphQL API) a vypíše přehled
produktů do stdout, který dashboard zobrazí jako "sonda".
"""
import sys

from itesco_parser import parse_flyer

MAX_CHARS = 17000  # limit výstupu dashboardu je 20 000 znaků


def cut(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


data = parse_flyer()
meta = data.get("meta", {})
products = data.get("products", [])

print()
print("=" * 60)
print("Obchod:   ", meta.get("store"))
print("Leták:    ", meta.get("flyer_title"))
print("Platnost: ", meta.get("valid_from"), "→", meta.get("valid_to"))
print("Stránek:  ", meta.get("total_pages"), "| produktů:", len(products))
print("URL:      ", meta.get("flyer_url"))
if meta.get("error"):
    print("CHYBA:    ", meta["error"])
print("=" * 60)

used = 0
shown = 0
for item in sorted(products, key=lambda p: (p.get("page") or 0, p.get("title") or "")):
    line = "s.{:>2} | {} | {}".format(
        item.get("page") or "?",
        cut(item.get("title"), 55),
        item.get("price") or "bez ceny",
    )
    if item.get("description"):
        line += " | " + cut(item["description"], 40)
    if used + len(line) > MAX_CHARS:
        break
    print(line)
    used += len(line) + 1
    shown += 1

if shown < len(products):
    print("… a dalších {} produktů (úplná data: itesco_rajhrad_data.json)".format(len(products) - shown))

# Nenulový exit = dashboard ukáže stav FAILED, ale výstup (včetně diagnostiky) zůstane vidět.
if not products or meta.get("error"):
    sys.exit(2)
