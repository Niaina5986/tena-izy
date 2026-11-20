import os
import json
import urllib.request
import urllib.parse
import urllib.error
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
        method="POST" if data is not None else "GET"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:

        body = e.read().decode("utf-8", errors="replace")

        raise RuntimeError(
            f"Telegram HTTP {e.code}: {body}"
        )

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result["result"]


def answer_callback(callback_id, message):

    if not callback_id:
        return

    try:
        telegram_request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": message
            }
        )

        log("Réponse au bouton envoyée.")

    except Exception as e:

        # Une réponse tardive ne doit pas bloquer l'approbation.
        log("Impossible de fermer le chargement du bouton : " + str(e))


def send_message(message):

    try:
        telegram_request(
            "sendMessage",
            {
                "chat_id": CHAT_ID,
                "text": message
            }
        )

        log("Message de confirmation envoyé.")

    except Exception as e:

        log("Erreur envoi message Telegram : " + str(e))


def find_mission(db, mission_id):

    return next(
        (
            m for m in db.get("missions", [])
            if str(m.get("id")) == str(mission_id)
        ),
        None
    )


def process_callback(db, update):

    callback = update.get("callback_query")

    if not callback:
        return False

    callback_id = callback.get("id")

    sender_id = str(
        callback.get("from", {}).get("id", "")
    )

    chat_id = str(
        callback.get("message", {})
        .get("chat", {})
        .get("id", "")
    )

    update_id = int(update.get("update_id", 0))

    # Vérification de sécurité.
    if sender_id != CHAT_ID or chat_id != CHAT_ID:

        log("Clic refusé : utilisateur non autorisé.")

        answer_callback(
            callback_id,
            "Accès refusé."
        )

        return True

    data = callback.get("data", "")

    try:
        action, mission_id = data.split(":", 1)

    except ValueError:

        answer_callback(
            callback_id,
            "Commande invalide."
        )

        return True

    mission = find_mission(db, mission_id)

    if not mission:

        log("Mission introuvable : " + mission_id)

        answer_callback(
            callback_id,
            "Mission introuvable."
        )

        return True

    if mission.get("status") != "WAITING_APPROVAL":

        log(
            "Mission déjà traitée : "
            + str(mission.get("status"))
        )

        answer_callback(
            callback_id,
            "Cette mission a déjà été traitée."
        )

        return True

    if action == "approve":

        mission["status"] = "APPROVED"
        mission["approved_at"] = now()

        db.setdefault("system", {})

        db["system"]["status"] = "APPROVED"
        db["system"]["current_mission"] = mission_id

        db.setdefault("history", [])

        db["history"].append({
            "mission_id": mission_id,
            "action": "APPROVED",
            "timestamp": now()
        })

        log("MISSION APPROUVÉE : " + mission_id)

        confirmation = (
            "✅ MISSION APPROUVÉE !\n\n"
            + str(mission.get("title", "Mission"))
            + "\n\n"
            "Manus pourra recevoir cette mission "
            "au prochain cycle d'exécution."
        )

        result_message = "Mission approuvée."

    elif action == "reject":

        mission["status"] = "REJECTED"
        mission["rejected_at"] = now()

        db.setdefault("system", {})

        db["system"]["status"] = "IDLE"
        db["system"]["current_mission"] = None

        db.setdefault("history", [])

        db["history"].append({
            "mission_id": mission_id,
            "action": "REJECTED",
            "timestamp": now()
        })

        log("MISSION REFUSÉE : " + mission_id)

        confirmation = (
            "❌ MISSION REFUSÉE\n\n"
            + str(mission.get("title", "Mission"))
        )

        result_message = "Mission refusée."

    else:

        answer_callback(
            callback_id,
            "Action inconnue."
        )

        return True

    # Enregistrer la décision AVANT de répondre à Telegram.
    db.setdefault("telegram", {})
    db["telegram"]["last_update_id"] = update_id

    save_db(db)

    log("Décision sauvegardée dans db.json.")

    # Ces opérations ne doivent jamais annuler la sauvegarde.
    answer_callback(
        callback_id,
        result_message
    )

    send_message(confirmation)

    return True


def run():

    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant."
        )

    db = load_db()

    db.setdefault("missions", [])
    db.setdefault("system", {})
    db.setdefault("telegram", {})
    db.setdefault("history", [])

    last_update = int(
        db["telegram"].get("last_update_id", 0)
    )

    log("Recherche des décisions Telegram...")

    updates = telegram_request(
        "getUpdates",
        {
            "offset": last_update + 1,
            "timeout": 0,
            "allowed_updates": json.dumps(["callback_query"])
        }
    )

    log(f"{len(updates)} mise(s) à jour reçue(s).")

    processed = 0

    for update in updates:

        update_id = int(update.get("update_id", 0))

        if update_id <= last_update:
            continue

        try:

            process_callback(db, update)

            db.setdefault("telegram", {})
            db["telegram"]["last_update_id"] = update_id

            save_db(db)

            last_update = update_id
            processed += 1

        except Exception as e:

            log(
                f"Erreur traitement update {update_id} : {e}"
            )

            raise

    log(
        f"Terminé : {processed} mise(s) à jour traitée(s)."
    )


if __name__ == "__main__":
    run()