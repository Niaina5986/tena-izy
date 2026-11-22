import os
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_URL = "https://api.manus.ai/v2/task.create"


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print("[MANUS WORKER]", message, flush=True)


def load_db():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    if not isinstance(db, dict):
        raise RuntimeError("Format db.json invalide.")

    db.setdefault("missions", [])
    db.setdefault("system", {})

    return db


def save_db(db):
    db["updated_at"] = now()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)


def notify(message):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram non configuré.")
        return

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            log("Erreur Telegram : " + str(result))

    except Exception as e:
        log("Notification impossible : " + str(e))


def create_manus_task(mission):

    title = mission.get("title", "Mission sans titre")
    description = mission.get("description", "")
    url = mission.get("url", "")

    prompt = f"""
Tu es l'agent d'exécution du système TENA IZY.

MISSION :
{title}

DESCRIPTION :
{description}

SOURCE :
{url}

OBJECTIFS :

1. Analyse la mission.
2. Vérifie les conditions et les prérequis.
3. Décompose le travail en étapes.
4. Exécute toutes les actions techniquement réalisables.
5. Prépare les fichiers et les livrables.
6. Vérifie les résultats et les tests.
7. Signale les éventuels blocages.

RÈGLES :

- Ne prétends jamais avoir réalisé une action non effectuée.
- Ne dépense aucun argent.
- N'utilise pas de faux comptes.
- Ne contourne aucune vérification humaine.
- Ne soumets pas automatiquement un travail à une plateforme.
- Ne fusionne aucune modification dans une branche principale.
- Si une intervention humaine est obligatoire, explique pourquoi.

À LA FIN, fournis :
- Résumé du travail.
- Fichiers créés ou modifiés.
- Tests réalisés.
- Résultats.
- Problèmes rencontrés.
- Actions humaines restantes.

État final :
READY_FOR_REVIEW, BLOCKED ou NEEDS_HUMAN.
"""

    payload = {
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                    "visibility": "visible"
                }
            ]
        },
        "title": "TENA IZY - " + title[:70],
        "locale": "fr",
        "agent_profile": "standard",
        "interactive_mode": False,
        "share_visibility": "private"
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-manus-api-key": MANUS_API_KEY
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Manus HTTP {e.code}: {details[:800]}"
        )

    if not result.get("ok") or not result.get("task_id"):
        raise RuntimeError("Réponse Manus invalide : " + str(result))

    return result


def run():

    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY manquante.")

    db = load_db()
    missions = db["missions"]

    active = [
        m for m in missions
        if m.get("status") == "IN_PROGRESS"
        and m.get("execution", {}).get("task_id")
    ]

    if active:
        log("Une mission est déjà en cours.")
        return

    approved = [
        m for m in missions
        if m.get("status") == "APPROVED"
        and not m.get("execution", {}).get("task_id")
    ]

    if not approved:
        log("Aucune mission approuvée.")
        return

    mission = approved[0]

    mission_id = str(mission["id"])

    log("Transmission à Manus : " + mission_id)

    result = create_manus_task(mission)

    mission["status"] = "IN_PROGRESS"

    mission["execution"] = {
        "status": "RUNNING",
        "executor": "MANUS",
        "task_id": str(result["task_id"]),
        "task_url": result.get("task_url"),
        "started_at": now(),
        "result": None,
        "submitted": False
    }

    db["system"]["status"] = "IN_PROGRESS"
    db["system"]["current_mission"] = mission_id

    save_db(db)

    message = (
        "🚀 MANUS A REÇU UNE MISSION\n\n"
        + str(mission.get("title", "Mission"))
        + "\n\nID : " + mission_id
        + "\nTask ID : " + str(result["task_id"])
        + "\nLien : " + str(result.get("task_url"))
    )

    notify(message)

    log("Mission envoyée avec succès.")


if __name__ == "__main__":
    run()