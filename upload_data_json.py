#!/usr/bin/env python3
"""Upload dashboard JSON files to Webzdarma using FTP temporary-file replacement."""
import ftplib
import io
import os
import sys
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def upload_one(ftp: ftplib.FTP, local_name: str, remote_name: str, remote_dir: str) -> None:
    path = Path(local_name)
    if not path.is_file():
        print(f"Přeskakuji {local_name}: soubor neexistuje.")
        return
    ftp.cwd("/")
    for part in [part for part in remote_dir.split("/") if part]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)
    temp_name = remote_name + ".part"
    payload = path.read_bytes()
    ftp.storbinary(f"STOR {temp_name}", io.BytesIO(payload))
    try:
        ftp.delete(remote_name)
    except ftplib.error_perm:
        pass
    ftp.rename(temp_name, remote_name)
    print(f"Nahráno {local_name} -> {remote_dir}/{remote_name} ({len(payload)} B)")


def main() -> int:
    host, user, password = env("FTP_SERVER"), env("FTP_USERNAME"), os.environ.get("FTP_PASSWORD", "")
    if not (host and user and password):
        print("Chybí FTP_SERVER, FTP_USERNAME nebo FTP_PASSWORD.", file=sys.stderr)
        return 1
    ftp = ftplib.FTP(timeout=30)
    try:
        ftp.connect(host, 21)
        ftp.login(user, password)
        ftp.set_pasv(True)
        remote_dir = env("FTP_REMOTE_DIR", "pipeline")
        upload_one(ftp, "data.json", "data.json", remote_dir)
        upload_one(ftp, "live_status.json", "live_status.json", remote_dir)
        return 0
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


if __name__ == "__main__":
    raise SystemExit(main())
