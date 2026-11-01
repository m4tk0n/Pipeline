#!/usr/bin/env python3
# Cesta: Pipeline/ftp_util.py
"""Sdílené FTP funkce pro agent_runner.py a upload_data_json.py."""
from __future__ import annotations

import ftplib
import io
import os
from pathlib import Path


def ftp_credentials() -> tuple[str, str, str]:
    return (
        os.getenv("FTP_SERVER", "").strip(),
        os.getenv("FTP_USERNAME", "").strip(),
        os.getenv("FTP_PASSWORD", ""),
    )


def connect(timeout: int = 20) -> ftplib.FTP:
    """Připojí se přes FTPS (výchozí). FTP_USE_TLS=0 povolí nešifrované FTP."""
    host, user, password = ftp_credentials()
    if not (host and user and password):
        raise RuntimeError("Chybí FTP_SERVER, FTP_USERNAME nebo FTP_PASSWORD.")

    use_tls = os.getenv("FTP_USE_TLS", "1").strip() != "0"
    ftp: ftplib.FTP = ftplib.FTP_TLS(timeout=timeout) if use_tls else ftplib.FTP(timeout=timeout)
    ftp.connect(host, 21)
    ftp.login(user, password)
    if use_tls:
        ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def cd_remote_dir(ftp: ftplib.FTP, remote_dir: str) -> None:
    ftp.cwd("/")
    for part in (p for p in remote_dir.split("/") if p):
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def upload_bytes(ftp: ftplib.FTP, payload: bytes, remote_name: str) -> None:
    """Nahraje do dočasného souboru a přejmenuje. Smaže cíl až když rename selže,
    aby soubor nezmizel (zbytečně) na dobu mezi delete a rename."""
    temp_name = remote_name + ".part"
    ftp.storbinary(f"STOR {temp_name}", io.BytesIO(payload))
    try:
        ftp.rename(temp_name, remote_name)
    except ftplib.error_perm:
        try:
            ftp.delete(remote_name)
        except ftplib.error_perm:
            pass
        ftp.rename(temp_name, remote_name)


def upload_file(ftp: ftplib.FTP, local: Path, remote_name: str) -> int:
    payload = local.read_bytes()
    upload_bytes(ftp, payload, remote_name)
    return len(payload)


def close(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:
        ftp.close()
