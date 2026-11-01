#!/usr/bin/env python3
"""Multi-turn Gemini agent with a restricted file editor and live FTP status."""
from __future__ import annotations

import datetime as dt
import ftplib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
from config import load_dotenv

load_dotenv(ROOT / ".env")

STATUS_FILE = ROOT / "live_status.json"
DATA_FILE = ROOT / "data.json"
PARSER_FILE = ROOT / "itesco_parser.py"
PARSER_OUTPUT = ROOT / "itesco_rajhrad_data.json"
ALLOWED_FILES = {"itesco_parser.py": PARSER_FILE}
MAX_TURNS = min(max(int(os.getenv("MAX_TURNS", "15")), 1), 15)
TOTAL_TIMEOUT = max(30, int(os.getenv("AGENT_TIMEOUT_SECONDS", "600")))
TEST_TIMEOUT = max(5, int(os.getenv("TEST_TIMEOUT_SECONDS", "45")))
MAX_LOG_ENTRIES = max(20, min(2000, int(os.getenv("MAX_LOG_ENTRIES", "250"))))
MIN_TESCO_PRODUCTS = max(1, int(os.getenv("MIN_TESCO_PRODUCTS", "1")))
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FTP_DIR = os.getenv("FTP_REMOTE_DIR", "pipeline").strip("/") or "pipeline"

STATUS: dict[str, Any] = {}
START_MONOTONIC = 0.0


def configuration_report() -> str:
    """
    Vypíše skutečnou konfiguraci agenta.
    Hodnoty tajných údajů nikdy nezobrazuje.
    """

    settings = [
        ("GEMINI_MODEL", GEMINI_MODEL),
        ("MAX_TURNS", MAX_TURNS),
        ("AGENT_TIMEOUT_SECONDS", TOTAL_TIMEOUT),
        ("TEST_TIMEOUT_SECONDS", TEST_TIMEOUT),
        ("MAX_LOG_ENTRIES", MAX_LOG_ENTRIES),
        ("MIN_TESCO_PRODUCTS", MIN_TESCO_PRODUCTS),
        ("FTP_REMOTE_DIR", FTP_DIR),
    ]

    lines = [
        "=== KONFIGURACE AI AGENTA ===",
    ]

    for name, value in settings:
        lines.append(f"{name}: {value}")

    lines.append("")
    lines.append("=== STAV TAJNÝCH PROMĚNNÝCH ===")

    secrets = [
        "GEMINI_API_KEY",
        "FTP_SERVER",
        "FTP_USERNAME",
        "FTP_PASSWORD",
    ]

    for name in secrets:
        configured = bool(os.getenv(name, "").strip())
        state = "NASTAVENO" if configured else "CHYBÍ"
        lines.append(f"{name}: [{state}]")

    lines.append("=============================")

    return "\n".join(lines)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_text(value: Any, limit: int = 12000) -> str:
    text = str(value or "")
    for secret_name in ("GEMINI_API_KEY", "FTP_PASSWORD", "GITHUB_TOKEN"):
        secret = os.getenv(secret_name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:limit]


def ftp_upload_status() -> None:
    host = os.getenv("FTP_SERVER", "").strip()
    user = os.getenv("FTP_USERNAME", "").strip()
    password = os.getenv("FTP_PASSWORD", "")
    if not (host and user and password):
        return

    payload = STATUS_FILE.read_bytes()
    ftp = ftplib.FTP(timeout=20)
    try:
        ftp.connect(host, 21)
        ftp.login(user, password)
        ftp.set_pasv(True)
        ftp.cwd("/")
        for part in FTP_DIR.split("/"):
            if not part:
                continue
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                ftp.mkd(part)
                ftp.cwd(part)
        temp_name = "live_status.json.part"
        with __import__("io").BytesIO(payload) as stream:
            ftp.storbinary(f"STOR {temp_name}", stream)
        try:
            ftp.delete("live_status.json")
        except ftplib.error_perm:
            pass
        ftp.rename(temp_name, "live_status.json")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def write_status(event: str, detail: str = "", **fields: Any) -> None:
    STATUS["last_update"] = now()
    STATUS["current_activity"] = safe_text(event, 500)
    if detail:
        STATUS.setdefault("logs", []).append(
            {"time": now(), "event": safe_text(event, 300), "detail": safe_text(detail, 5000)}
        )
        STATUS["logs"] = STATUS["logs"][-MAX_LOG_ENTRIES:]
    STATUS.update(fields)

    # Local atomic replacement prevents readers from seeing a partial JSON document.
    temp = STATUS_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(STATUS, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATUS_FILE)

    try:
        ftp_upload_status()
    except Exception as exc:
        print(f"[LIVE FTP WARNING] {safe_text(exc, 1000)}", flush=True)


def load_task() -> dict[str, Any]:
    raw = os.getenv("TASK_JSON", "").strip()
    if raw:
        task = json.loads(raw)
    elif (ROOT / "task.json").exists():
        task = json.loads((ROOT / "task.json").read_text(encoding="utf-8"))
    else:
        task = {
            "task": "Analyzuj a oprav parser aktuálního letáku Tesco Rajhrad tak, aby získával skutečné produkty. Zachovej existující výstupní JSON strukturu.",
            "target_file": "itesco_parser.py",
            "validation": {"type": "tesco_products", "min_products": MIN_TESCO_PRODUCTS},
        }

    if isinstance(task, str):
        task = {"task": task}

    if not isinstance(task, dict) or not str(task.get("task", "")).strip():
        raise ValueError("Zadání musí být JSON objekt s neprázdným polem 'task'.")

    target = task.get("target_file", "itesco_parser.py")
    if target not in ALLOWED_FILES:
        raise ValueError(f"Nepovolený cílový soubor: {target}")

    task["target_file"] = target
    task.setdefault("validation", {"type": "tesco_products", "min_products": MIN_TESCO_PRODUCTS})

    if isinstance(task["validation"], dict) and task["validation"].get("type") == "tesco_products":
        task["validation"].setdefault("min_products", MIN_TESCO_PRODUCTS)

    return task


def gemini_request(contents: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Chybí GitHub Secret GEMINI_API_KEY.")

    url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)

    response = requests.post(
        url,
        params={"key": api_key},
        json={
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "action": {
                            "type": "STRING",
                            "enum": ["modify_code", "run_test", "analyze_only", "finish"],
                        },
                        "target_file": {"type": "STRING"},
                        "explanation": {"type": "STRING"},
                        "code_content": {"type": "STRING"},
                        "edits": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "old_text": {"type": "STRING"},
                                    "new_text": {"type": "STRING"},
                                },
                                "required": ["old_text", "new_text"],
                            },
                        },
                        "task_completed": {"type": "BOOLEAN"},
                    },
                    "required": ["action", "explanation", "task_completed"],
                },
            },
        },
        timeout=(15, 90),
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini API HTTP {response.status_code}: {safe_text(response.text, 3000)}"
        )

    body = response.json()

    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Gemini vrátilo prázdnou/neplatnou odpověď: {safe_text(body, 2000)}"
        ) from exc

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini nevrátilo validní JSON: {safe_text(text, 2000)}"
        ) from exc

    if not isinstance(result, dict) or result.get("action") not in {
        "modify_code", "run_test", "analyze_only", "finish"
    }:
        raise RuntimeError("Gemini vrátilo neplatnou akci.")

    return result


def apply_model_changes(result: dict[str, Any], target_name: str) -> None:
    if result.get("target_file", target_name) != target_name:
        raise ValueError("Gemini se pokusilo upravit jiný než povolený soubor.")

    target = ALLOWED_FILES[target_name]
    original = target.read_text(encoding="utf-8")

    if isinstance(result.get("edits"), list) and result["edits"]:
        updated = original

        for edit in result["edits"]:
            old = edit.get("old_text", "")
            new = edit.get("new_text", "")

            if not old or updated.count(old) != 1:
                raise ValueError(
                    "Patch odmítnut: old_text musí odpovídat právě jednomu místu v souboru."
                )

            updated = updated.replace(old, new, 1)

    elif isinstance(result.get("code_content"), str) and result["code_content"].strip():
        updated = result["code_content"]

    else:
        raise ValueError("Akce modify_code neobsahuje edits ani code_content.")

    if updated == original:
        raise ValueError("Gemini navrhlo změnu, která nemění obsah souboru.")

    backup = target.with_suffix(target.suffix + ".bak")
    backup.write_text(original, encoding="utf-8")

    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(updated, encoding="utf-8")
    temp.replace(target)

    STATUS.setdefault("changes", []).append(
        {"file": target_name, "change": "updated", "time": now()}
    )
    STATUS["changes"] = STATUS["changes"][-20:]


def run_allowed_test(validation: dict[str, Any]) -> dict[str, Any]:
    command = [sys.executable, str(PARSER_FILE)]

    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode

    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = (
            safe_text(exc.stdout),
            "Test překročil časový limit.",
            124,
        )

    data = None
    json_error = None

    if PARSER_OUTPUT.exists():
        try:
            data = json.loads(PARSER_OUTPUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            json_error = str(exc)
    else:
        json_error = "Parser nevytvořil itesco_rajhrad_data.json."

    valid = (
        isinstance(data, dict)
        and isinstance(data.get("meta"), dict)
        and isinstance(data.get("products"), list)
    )

    validation_type = validation.get("type", "tesco_products")

    if validation_type == "tesco_products":
        minimum = max(1, int(validation.get("min_products", MIN_TESCO_PRODUCTS)))
        valid = bool(valid and len(data["products"]) >= minimum and code == 0)

        reason = (
            f"Požadováno alespoň {minimum} produktů; "
            f"nalezeno {len(data['products']) if valid or isinstance(data, dict) and isinstance(data.get('products'), list) else 0}."
        )
    else:
        valid = False
        reason = f"Neznámý validační profil: {validation_type}."

    return {
        "command": "python itesco_parser.py",
        "exit_code": code,
        "success": bool(valid),
        "stdout": safe_text(stdout, 5000),
        "stderr": safe_text(stderr, 5000),
        "json_error": safe_text(json_error, 1000),
        "products_count": (
            len(data.get("products", []))
            if isinstance(data, dict) and isinstance(data.get("products"), list)
            else 0
        ),
        "reason": reason,
    }


def make_dashboard_data(
    status: str,
    task: str,
    result: dict[str, Any] | None = None,
) -> None:
    logs = [
        f"[{item['time']}] {item['event']}: {item['detail']}"
        for item in STATUS.get("logs", [])[-100:]
    ]

    history = STATUS.get("turns_history", [])
    failed_attempts = sum(
        1 for item in history if item.get("test") and not item["test"].get("success")
    )

    progress = (
        100
        if status == "SUCCESS"
        else round(
            100 * STATUS.get("current_turn", 0)
            / max(1, STATUS.get("max_turns", 1))
        )
    )

    dashboard = {
        "status": status,
        "last_run": STATUS.get("last_update", ""),
        "tasks_completed": 1 if status == "SUCCESS" else 0,
        "commits_count": len(STATUS.get("commits", [])),
        "errors_resolved": failed_attempts if status == "SUCCESS" else 0,
        "current_task": task,
        "task_progress": progress,
        "task_time": "",
        "logs": logs,
        "commits": STATUS.get("commits", []),
        "conversation": STATUS.get("conversation", [])[-100:],
    }

    DATA_FILE.write_text(
        json.dumps(dashboard, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_agent() -> int:
    global STATUS, START_MONOTONIC

    START_MONOTONIC = time.monotonic()
    session_id = (
        dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        + "_"
        + uuid.uuid4().hex[:6]
    )

    STATUS = {
        "session_id": session_id,
        "status": "STARTING",
        "current_turn": 0,
        "max_turns": MAX_TURNS,
        "current_activity": "Načítám zadání",
        "start_time": now(),
        "last_update": now(),
        "turns_history": [],
        "conversation": [],
        "logs": [],
        "final_result": None,
        "changes": [],
        "commits": [],
    }

    try:
        task = load_task()
        task_text = json.dumps(task, ensure_ascii=False, indent=2)

        write_status(
            "Agent spuštěn",
            f"Zadání načteno. Cílový soubor: {task['target_file']}",
            status="RUNNING",
        )

        # Výpis skutečné konfigurace do GitHub Actions i živého logu.
        config_text = configuration_report()

        print(config_text, flush=True)

        write_status(
            "Konfigurace agenta",
            config_text,
        )

        source_code = PARSER_FILE.read_text(encoding="utf-8")

        last_test: dict[str, Any] | None = run_allowed_test(task["validation"])

        write_status(
            "Výchozí test dokončen",
            json.dumps(last_test, ensure_ascii=False),
        )

        initial_prompt = (
            "Jsi programovací agent. Řeš úkol iterativně a odpovídej pouze JSON podle vynuceného schématu. "
            "Nevypisuj interní skryté uvažování; pole explanation je stručné shrnutí rozhodnutí. "
            "Povolený soubor je výhradně itesco_parser.py. Nikdy nežádej ani nevydávej libovolné shell příkazy. "
            "Povolené akce: modify_code, run_test, analyze_only, finish. "
            "Při modify_code použij přednostně edits [{old_text,new_text}], kde old_text přesně odpovídá právě jednomu místu; "
            "code_content použij jen když je nutný úplný obsah souboru. Test spouští orchestrátor sám. "
            "Dokončení smíš deklarovat pouze po úspěšném reálném testu.\n\nZADÁNÍ:\n"
            + task_text
            + "\n\nAKTUÁLNÍ OBSAH POVOLENÉHO SOUBORU itesco_parser.py:\n"
            + source_code
        )

        contents: list[dict[str, Any]] = [
            {"role": "user", "parts": [{"text": initial_prompt}]}
        ]

        STATUS["conversation"].append({
            "role": "user",
            "time": now(),
            "text": safe_text(
                f"Zadání: {task['task']}\n"
                f"Cílový soubor: {task['target_file']}\n"
                f"Validace: {task['validation']}",
                3000,
            ),
        })

        for turn in range(1, MAX_TURNS + 1):
            if time.monotonic() - START_MONOTONIC > TOTAL_TIMEOUT:
                raise TimeoutError(
                    "Byl překročen maximální čas celého běhu agenta."
                )

            STATUS["current_turn"] = turn

            write_status(
                f"Komunikační kolo {turn}/{MAX_TURNS}",
                "Odesílám kontext Gemini.",
            )

            if last_test is not None:
                feedback = (
                    "VÝSLEDEK POSLEDNÍHO TESTU (skutečný výstup):\n"
                    + json.dumps(last_test, ensure_ascii=False)
                )

                contents.append({
                    "role": "user",
                    "parts": [{"text": feedback}],
                })

                STATUS["conversation"].append({
                    "role": "user",
                    "time": now(),
                    "text": feedback,
                })

            model_result = gemini_request(contents)
            model_json = json.dumps(model_result, ensure_ascii=False)

            contents.append({
                "role": "model",
                "parts": [{"text": model_json}],
            })

            STATUS["conversation"].append({
                "role": "model",
                "time": now(),
                "text": safe_text(
                    json.dumps({
                        "action": model_result.get("action"),
                        "target_file": model_result.get(
                            "target_file",
                            task["target_file"],
                        ),
                        "explanation": model_result.get("explanation", ""),
                        "task_completed": model_result.get(
                            "task_completed",
                            False,
                        ),
                    }, ensure_ascii=False),
                    3000,
                ),
            })

            STATUS["conversation"] = STATUS["conversation"][-100:]

            action = model_result["action"]
            explanation = safe_text(
                model_result.get("explanation", ""),
                3000,
            )

            turn_record: dict[str, Any] = {
                "turn": turn,
                "action": action,
                "explanation": explanation,
                "time": now(),
            }

            write_status(f"Gemini: {action}", explanation)

            if action == "modify_code":
                try:
                    apply_model_changes(model_result, task["target_file"])

                    write_status(
                        "Kód upraven",
                        f"Gemini změnilo {task['target_file']}. Spouštím povolený test.",
                    )

                    last_test = run_allowed_test(task["validation"])

                    if not last_test.get("success"):
                        backup = PARSER_FILE.with_suffix(
                            PARSER_FILE.suffix + ".bak"
                        )

                        if backup.exists():
                            backup.replace(PARSER_FILE)

                            write_status(
                                "Rollback",
                                "Test neprošel; vracím poslední změnu a předávám výsledek Gemini.",
                            )

                except Exception as change_error:
                    last_test = {
                        "success": False,
                        "reason": (
                            "Změna kódu byla odmítnuta: "
                            + safe_text(change_error, 2000)
                        ),
                    }

                    write_status(
                        "Změna odmítnuta",
                        last_test["reason"],
                    )

            elif action == "run_test":
                last_test = run_allowed_test(task["validation"])

            elif action == "analyze_only":
                last_test = {
                    "success": False,
                    "reason": "Pouze analýza; úkol zatím není dokončen.",
                }

            elif action == "finish":
                if (
                    last_test
                    and last_test.get("success")
                    and model_result.get("task_completed") is True
                ):
                    turn_record["test"] = last_test
                    STATUS["turns_history"].append(turn_record)
                    STATUS["status"] = "SUCCESS"
                    STATUS["final_result"] = {
                        "success": True,
                        "message": explanation,
                    }

                    write_status(
                        "Úkol dokončen",
                        explanation,
                        status="SUCCESS",
                    )

                    make_dashboard_data(
                        "SUCCESS",
                        str(task["task"]),
                        last_test,
                    )

                    return commit_successful_parser(task["target_file"])

                last_test = {
                    "success": False,
                    "reason": (
                        "Gemini se pokusilo ukončit bez úspěšného testu. "
                        "Dokončení odmítnuto."
                    ),
                }

            turn_record["test"] = last_test
            STATUS["turns_history"].append(turn_record)

            write_status(
                "Test dokončen" if last_test else "Čekám na další krok",
                json.dumps(last_test, ensure_ascii=False) if last_test else "",
            )

            make_dashboard_data(
                "RUNNING",
                str(task["task"]),
                last_test,
            )

            time.sleep(1)

        STATUS["status"] = "MAX_TURNS"
        STATUS["final_result"] = {
            "success": False,
            "message": "Dosažen maximální počet komunikačních kol.",
        }

        write_status(
            "Dosažen limit kol",
            "Úkol nebyl potvrzen jako dokončený.",
            status="MAX_TURNS",
        )

        make_dashboard_data(
            "WARNING",
            str(task["task"]),
            last_test,
        )

        return 1

    except Exception as exc:
        STATUS["status"] = "ERROR"
        STATUS["final_result"] = {
            "success": False,
            "message": safe_text(exc, 3000),
        }

        write_status(
            "Chyba agenta",
            safe_text(exc, 3000),
            status="ERROR",
        )

        make_dashboard_data(
            "ERROR",
            "Agent skončil chybou.",
        )

        return 1


def commit_successful_parser(target_name: str) -> int:
    """Commit only a tested parser change; never deploy parser code via FTP."""
    try:
        subprocess.run(
            ["git", "config", "user.name", "AI Agent Bot"],
            cwd=ROOT,
            check=True,
        )

        subprocess.run(
            ["git", "config", "user.email", "agent@users.noreply.github.com"],
            cwd=ROOT,
            check=True,
        )

        subprocess.run(
            ["git", "add", target_name, "itesco_rajhrad_data.json"],
            cwd=ROOT,
            check=True,
        )

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=ROOT,
        )

        if diff.returncode == 1:
            subprocess.run(
                ["git", "commit", "-m", "AI Agent: tested Tesco parser update"],
                cwd=ROOT,
                check=True,
            )

            subprocess.run(
                ["git", "push"],
                cwd=ROOT,
                check=True,
            )

            short_hash = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            STATUS.setdefault("commits", []).append({
                "msg": "AI Agent: tested Tesco parser update",
                "hash": short_hash,
            })

            write_status(
                "Ověřená změna uložena do GitHubu",
                f"Commit a push proběhly po úspěšném testu: {short_hash}.",
            )

        elif diff.returncode not in (0, 1):
            raise RuntimeError(
                "Nepodařilo se ověřit staged změny."
            )

        make_dashboard_data(
            "SUCCESS",
            "Tesco parser – úspěšně otestováno",
        )

        return 0

    except Exception as exc:
        write_status(
            "Git chyba",
            safe_text(exc, 2000),
            status="GIT_ERROR",
        )

        make_dashboard_data(
            "WARNING",
            "Test prošel, ale uložení do GitHubu selhalo.",
        )

        return 1


if __name__ == "__main__":
    sys.exit(run_agent())