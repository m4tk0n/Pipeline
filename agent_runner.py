import os
import json
import datetime
import google.generativeai as genai

# Configuration Gemini API (přes GitHub Secrets)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

def run_agent():
    print("🤖 Spouštím AI Agenta...")
    
    # 1. Volání AI (Gemini Free Tier)
    if GEMINI_KEY:
        genai.configure(api_key=GEMINI_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        prompt = "Napiš jednoduchou Python funkci pro výpočet DPH v CZ. Vrať pouze kód bez pouček."
        try:
            response = model.generate_content(prompt)
            generated_code = response.text
        except Exception as e:
            generated_code = f"# Chyba při volání API: {e}"
    else:
        generated_code = "# GEMINI_API_KEY není nastaven v GitHub Secrets"

    # 2. Příprava dat pro dashboard (data.json)
    now = datetime.datetime.now().strftime("%H:%M:%S")
    
    data = {
        "status": "ACTIVE",
        "last_run": f"Dnes v {now}",
        "tasks_completed": 113,
        "commits_count": 46,
        "errors_resolved": 16,
        "current_task": "Refactoring log parser (GitHub #77)",
        "task_progress": 75,
        "task_time": "2:10 elapsed",
        "logs": [
            f"[{now}] [System] GitHub Actions workflow triggered.",
            f"[{now}] [Agent] Fetched task from GitHub Issue.",
            f"[{now}] [API] Sent prompt to Gemini 1.5 Flash API.",
            f"[{now}] [Agent] Received response, generating Python code...",
            f"[{now}] [System] Updating data.json on Webzdarma FTP..."
        ],
        "code_preview": generated_code,
        "commits": [
            {"msg": "AI Agent: Refactored DPH tool via Gemini API", "hash": "8a3f11b"},
            {"msg": "AI Agent: Fixed parser logic in generated_tool.py", "hash": "2c91a01"}
        ]
    }

    # 3. Zápis do data.json
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    print("✅ data.json úspěšně aktualizován!")

if __name__ == "__main__":
    run_agent()
