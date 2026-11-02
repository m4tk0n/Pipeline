#!/usr/bin/env python3
# Cesta: Pipeline/custom_runner.py
"""Zadání z dashboardu (popis úkolu + kód + název souboru). Bez AI, vždy jeden krok.

1. Stáhne custom_task.json ze serveru (FTP).
2. Pokud je vyplněný kód, zapíše ho do souboru (přepíše původní).
3. Soubor spustí.
4. Výstup běhu ("sonda") uloží do custom_result.json a nahraje na server.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import agent_runner as ar
import ftp_util
from agent_runner import ROOT, now, safe_text

TASK_REMOTE = "custom_task.json"
RESULT_REMOTE = "custom_result.json"
RESULT_LOCAL = ROOT / RESULT_REMOTE

RUN_TIMEOUT = max(5, ar._int_env("CUSTOM_RUN_TIMEOUT_SECONDS", 60))
MAX_AI_TURNS = min(ar.MAX_TURNS, max(1, ar._int_env("CUSTOM_MAX_TURNS", 8)))
MAX_SONDA = 20000

FILE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,59}\.py$")
# Soubory, které by po přepsání mohly ohrozit samotného agenta a jeho tajemství.
PROTECTED = {
    "agent_runner.py", "custom_runner.py", "ftp_util.py", "config.py", "upload_data_json.py",
}
SHADOWING = {"requests", "urllib3", "certifi", "idna", "charset_normalizer"}



# --------------------------------------------------------------------------- pomocné

def check_file_name(name: str) -> str:
    name = (name or "").strip()
    if not FILE_RE.fullmatch(name):
        raise ValueError(
            "Název souboru musí vypadat např. tesco_cz.py "
            "(písmena, čísla, _ a -, přípona .py, bez složek)."
        )
    if name in PROTECTED:
        raise ValueError(f"Soubor {name} je chráněný a nelze ho přepsat.")
    stem = name[:-3]
    if stem in sys.stdlib_module_names or stem in SHADOWING:
        raise ValueError(f"Název {name} koliduje s knihovnou Pythonu; zvol jiný.")
    return name


def write_code(name: str, code: str) -> None:
    target = ROOT / name
    temp = target.with_suffix(".py.tmp")
    temp.write_text(code.replace("\r\n", "\n").replace("\r", "\n"), encoding="utf-8")
    temp.replace(target)


def download_task() -> dict[str, Any]:
    ftp = ftp_util.connect(timeout=30)
    try:
        ftp_util.cd_remote_dir(ftp, ar.FTP_DIR)
        buffer = io.BytesIO()
        ftp.retrbinary(f"RETR {TASK_REMOTE}", buffer.write)
    finally:
        ftp_util.close(ftp)
    task = json.loads(buffer.getvalue().decode("utf-8-sig"))
    if not isinstance(task, dict):
        raise ValueError("custom_task.json musí být JSON objekt.")
    return task


def publish(result: dict[str, Any]) -> None:
    """Uloží custom_result.json lokálně a nahraje ho na server (chyba neshodí běh)."""
    result["updated"] = now()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    RESULT_LOCAL.write_text(text, encoding="utf-8")
    for attempt in range(2):
        try:
            ftp = ftp_util.connect(timeout=30)
            try:
                ftp_util.cd_remote_dir(ftp, ar.FTP_DIR)
                ftp_util.upload_bytes(ftp, text.encode("utf-8"), RESULT_REMOTE)
            finally:
                ftp_util.close(ftp)
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[RESULT FTP WARNING] pokus {attempt + 1}: {safe_text(exc, 500)}", flush=True)
            time.sleep(2)


def run_file(name: str) -> dict[str, Any]:
    path = ROOT / name
    if not path.is_file():
        return {
            "file": name, "exit_code": 127, "stdout": "", "timed_out": False,
            "stderr": f"Soubor {name} neexistuje.", "duration_s": 0,
        }
    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=RUN_TIMEOUT, env=ar.safe_env(),
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code, timed_out = exc.stdout, f"Překročen časový limit {RUN_TIMEOUT} s.", 124, True
    return {
        "file": name,
        "exit_code": code,
        "stdout": safe_text(stdout, MAX_SONDA),
        "stderr": safe_text(stderr, 5000),
        "timed_out": timed_out,
        "duration_s": round(time.monotonic() - started, 1),
    }


def compose_sonda(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    parts = [str(run.get("stdout") or "").rstrip()]
    stderr = str(run.get("stderr") or "").strip()
    if stderr:
        parts.append("--- stderr ---\n" + stderr)
    return "\n\n".join(p for p in parts if p)[:MAX_SONDA]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)


def commit_files(names: list[str]) -> str:
    """Uloží otestované soubory do GitHubu. Vrací krátkou poznámku do výsledku."""
    try:
        git("config", "user.name", "AI Agent Bot")
        git("config", "user.email", "agent@users.noreply.github.com")
        git("add", *names)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if diff.returncode == 0:
            return "Repozitář: beze změny."
        if diff.returncode != 1:
            raise RuntimeError("Nepodařilo se ověřit staged změny.")
        git("commit", "-m", "Dashboard: " + ", ".join(names))
        git("push")
        short = git("rev-parse", "--short", "HEAD").stdout.strip()
        ar.STATUS.setdefault("commits", []).append({"msg": "Dashboard: " + ", ".join(names), "hash": short})
        return f"Uloženo do GitHubu ({short})."
    except Exception as exc:  # noqa: BLE001
        return "Uložení do GitHubu selhalo: " + safe_text(exc, 500)


# --------------------------------------------------------------------------- hlavní běh

def main() -> int:
    ar.START_MONOTONIC = time.monotonic()
    ar.STATUS = {
        "session_id": "custom_" + time.strftime("%Y%m%d_%H%M%S"),
        "status": "STARTING", "current_turn": 0, "max_turns": 1,
        "current_activity": "Stahuji zadání z dashboardu",
        "start_time": now(), "last_update": now(),
        "turns_history": [], "conversation": [], "logs": [],
        "final_result": None, "changes": [], "commits": [],
    }
    expected_id = os.getenv("RUN_ID", "").strip()
    started = now()

    try:
        task = download_task()
    except Exception as exc:  # noqa: BLE001
        message = "Zadání se nepodařilo stáhnout ze serveru: " + safe_text(exc, 800)
        publish({"run_id": expected_id, "status": "ERROR", "message": message, "file": "",
                 "sonda": "", "exit_code": None, "started": started, "finished": now()})
        print(message, flush=True)
        return 1

    run_id = str(task.get("run_id") or "")
    if expected_id and run_id != expected_id:
        print(f"Zadání {expected_id} bylo nahrazeno novějším ({run_id}); běh přeskočen.", flush=True)
        return 0

    description = str(task.get("task") or "").strip()
    code = str(task.get("code") or "")
    file_name = str(task.get("file") or "").strip()
    label = (description or f"Spuštění {file_name}")[:120]

    result: dict[str, Any] = {
        "run_id": run_id, "status": "RUNNING", "message": "Agent běží na GitHub Actions.",
        "file": file_name, "sonda": "", "exit_code": None, "started": started,
    }
    publish(result)
    ar.write_status("Agent spuštěn", f"Zadání {run_id} načteno.", status="RUNNING")

    status, message, run = "ERROR", "", None
    try:
        name = check_file_name(file_name)
        wrote = False
        if code.strip():
            write_code(name, code)
            wrote = True
            ar.write_status("Kód zapsán", f"{name}: {len(code)} znaků")

        run = run_file(name)
        ar.write_status("Soubor spuštěn", f"{name}: exit {run['exit_code']}, {run['duration_s']} s")
        status = "SUCCESS" if run["exit_code"] == 0 else "FAILED"
        message = "Soubor proběhl úspěšně." if status == "SUCCESS" else "Soubor skončil chybou."

        if status == "SUCCESS" and wrote:
            message += " " + commit_files([name])

    except Exception as exc:  # noqa: BLE001
        status = "ERROR"
        message = safe_text(exc, 3000)
        ar.write_status("Chyba agenta", message, force=True)

    result.update({
        "status": status,
        "message": message,
        "file": file_name,
        "exit_code": run["exit_code"] if run else None,
        "duration_s": run["duration_s"] if run else None,
        "sonda": compose_sonda(run),
        "finished": now(),
    })

    mapped = {"SUCCESS": "SUCCESS", "FAILED": "WARNING"}.get(status, "ERROR")
    ar.STATUS["status"] = mapped
    ar.STATUS["final_result"] = {"success": status == "SUCCESS", "message": message}
    ar.write_status("Hotovo", message, force=True, status=mapped)
    ar.make_dashboard_data(mapped, label)
    publish(result)
    return 1 if status == "ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())
