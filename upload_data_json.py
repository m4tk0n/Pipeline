#!/usr/bin/env python3
# Cesta: Pipeline/upload_data_json.py
"""Nahraje data.json a live_status.json na Webzdarma přes FTP(S)."""
import os
import sys
from pathlib import Path

from config import load_dotenv
import ftp_util

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

FILES = ("data.json", "live_status.json", "itesco_rajhrad_data.json", "custom_result.json")


def main() -> int:
    present = [name for name in FILES if (ROOT / name).is_file()]
    for name in FILES:
        if name not in present:
            print(f"Přeskakuji {name}: soubor neexistuje.")
    if not present:
        print("Není co nahrát: žádný z očekávaných souborů neexistuje.", file=sys.stderr)
        return 1

    remote_dir = (os.environ.get("FTP_REMOTE_DIR") or "pipeline").strip()
    try:
        ftp = ftp_util.connect(timeout=30)
    except Exception as exc:
        print(f"FTP připojení selhalo: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        ftp_util.cd_remote_dir(ftp, remote_dir)
        for name in present:
            size = ftp_util.upload_file(ftp, ROOT / name, name)
            print(f"Nahráno {name} -> {remote_dir}/{name} ({size} B)")
        return 0
    except Exception as exc:
        print(f"FTP upload selhal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        ftp_util.close(ftp)


if __name__ == "__main__":
    raise SystemExit(main())
