import os
import json
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
    print("[TELEGRAM APPROVAL]", message, flush=True)


def load_db():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    db["updated_at"] = now()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)


def telegram_request(method, payload=None):

    url = API + method

    data = None

    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(str(result))

    return result["result"]


def answer_callback(callback_id, message):

    telegram_request(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": message
        }
    )


def send_message(message):

    telegram_request(
        "sendMessage",
        {
            "chat_id": CHAT_ID,
            "text": message
        }
    )


def run():

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Configuration Telegram manquante.")

    db = load_db()

    db.setdefault("missions", [])
    db.setdefault("system", {})

    telegram_data = db.setdefault("telegram", {})

    last_update = int(
        telegram_data.get("last_update_id", 0)
    )

    updates = telegram_request(
        "getUpdates",
        {
            "offset": last_update + 1,
            "timeout": 0,
            "allowed_updates": json.dumps(["callback_query"])
        }
    )

    changed = False

    for update in updates:

        update_id = int(update.get("update_id", 0))

        callback = update.get("callback_query")

        if not callback:
            telegram_data["last_update_id"] = update_id
            changed = True
            continue

        callback_id = callback.get("id")

        sender_id = str(
            callback.get("from", {}).get("id", "")
        )

        chat_id = str(
            callback.get("message", {})
            .get("chat", {})
            .get("id", "")
        )

        # Sécurité : seul ton compte peut décider.
        if sender_id != CHAT_ID or chat_id != CHAT_ID:

            answer_callback(
                callback_id,
                "Accès refusé."
            )

            telegram_data["last_update_id"] = update_id
            changed = True
            continue

        data = callback.get("data", "")

        try:
            action, mission_id = data.split(":", 1)
        except ValueError:
            answer_callback(callback_id, "Commande invalide.")
            telegram_data["last_update_id"] = update_id
            changed = True
            continue

        mission = next(
            (
                m for m in db["missions"]
                if str(m.get("id")) == mission_id
            ),
            None
        )

        if not mission:

            answer_callback(callback_id, "Mission introuvable.")

            telegram_data["last_update_id"] = update_id
            changed = True
            continue

        if mission.get("status") != "WAITING_APPROVAL":

            answer_callback(
                callback_id,
                "Cette mission n'attend plus de validation."
            )

            telegram_data["last_update_id"] = update_id
            changed = True
            continue

        if action == "approve":

            mission["status"] = "APPROVED"
            mission["approved_at"] = now()

            db["system"]["status"] = "APPROVED"
            db["system"]["current_mission"] = mission_id

            answer_callback(
                callback_id,
                "Mission approuvée !"
            )

            send_message(
                "✅ Mission approuvée.\n"
                "Manus va recevoir la tâche au prochain cycle."
            )

            log("MISSION APPROUVÉE : " + mission_id)

        elif action == "reject":

            mission["status"] = "REJECTED"
            mission["rejected_at"] = now()

            db["system"]["status"] = "IDLE"
            db["system"]["current_mission"] = None

            answer_callback(
                callback_id,
                "Mission refusée."
            )

            send_message(
                "❌ Mission refusée.\n"
                "Le système pourra chercher une autre opportunité."
            )

            log("MISSION REFUSÉE : " + mission_id)

        else:

            answer_callback(callback_id, "Action inconnue.")

        telegram_data["last_update_id"] = update_id
        changed = True

    if changed:
        save_db(db)

    log(f"{len(updates)} mise(s) à jour examinée(s).")


if __name__ == "__main__":
    run()