import os
import json
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API = f"https://api.telegram.org/bot{BOT_TOKEN}/"


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print("[TELEGRAM NOTIFY]", message, flush=True)


def load_db():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    db["updated_at"] = now()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)


def telegram_request(method, payload):
    url = API + method

    data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(str(result))

    return result["result"]


def send_mission(mission):

    title = html.escape(mission.get("title", "Sans titre"))

    description = html.escape(
        mission.get("analysis", {}).get("explanation", "")
    )

    score = mission.get("score", 0)

    analysis = mission.get("analysis", {})

    reward = analysis.get("reward_estimate", 0)
    difficulty = analysis.get("difficulty", "INCONNUE")
    automation = analysis.get("automation", "INCONNUE")
    risk = analysis.get("risk", "INCONNU")

    url = mission.get("url", "")

    message = f"""
<b>🤖 BOT WORKER — MISSION À VALIDER</b>

<b>{title}</b>

━━━━━━━━━━━━━━━━━━

⭐ Score : {score}/10
💰 Récompense estimée : ${reward}
⚙️ Difficulté : {difficulty}
🤖 Automatisation : {automation}
⚠️ Risque : {risk}

<b>Analyse Gemini :</b>
{description}

━━━━━━━━━━━━━━━━━━

<a href="{html.escape(url, quote=True)}">Consulter la mission originale</a>

Choisis une action :
"""

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ APPROUVER",
                    "callback_data": f"approve:{mission['id']}"
                },
                {
                    "text": "❌ REFUSER",
                    "callback_data": f"reject:{mission['id']}"
                }
            ]
        ]
    }

    result = telegram_request(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard)
        }
    )

    return result["message_id"]


def run():

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant."
        )

    db = load_db()

    db.setdefault("missions", [])
    db.setdefault("system", {})

    sent = 0

    for mission in db["missions"]:

        if mission.get("status") != "WAITING_APPROVAL":
            continue

        telegram_data = mission.setdefault("telegram", {})

        if telegram_data.get("message_id"):
            continue

        log("Envoi : " + mission.get("title", ""))

        message_id = send_mission(mission)

        telegram_data["message_id"] = message_id
        telegram_data["sent_at"] = now()

        sent += 1

    save_db(db)

    log(f"{sent} notification(s) envoyée(s).")


if __name__ == "__main__":
    run()