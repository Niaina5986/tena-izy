import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

DB_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "db.json"
)

MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")

API_URL = "https://api.manus.ai/v2/task.create"


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print("[MANUS WORKER]", message)


def load_db():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    db["updated_at"] = now()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)


def create_manus_task(mission):

    title = mission.get("title", "Mission sans titre")
    description = mission.get("description", "")
    url = mission.get("url", "")

    prompt = f"""
Tu es l'agent d'exécution technique du système BOT WORKER.

MISSION :
{title}

DESCRIPTION :
{description}

SOURCE :
{url}

OBJECTIF :

1. Analyse précisément la mission.
2. Identifie les conditions préalables.
3. Vérifie les dépendances et les difficultés.
4. Décompose le travail en étapes.
5. Identifie les actions réalisables avec tes outils.
6. Prépare les modifications ou livrables nécessaires.
7. Vérifie techniquement les résultats.

RÈGLES OBLIGATOIRES :

- Ne prétends jamais avoir effectué une action non réalisée.
- Ne soumets aucun travail à une plateforme.
- Ne fusionne aucune modification dans la branche principale.
- Ne dépense aucun argent et n'effectue aucun achat.
- Ne contourne aucune vérification humaine.
- N'utilise pas de faux comptes ou de fausses identités.
- Si une information manque, demande une intervention humaine.
- Si les prérequis ne sont pas remplis, explique le blocage.

À LA FIN, FOURNIS :

- Résumé du travail effectué.
- Fichiers créés ou modifiés.
- Tests réalisés et résultats.
- Problèmes rencontrés.
- Actions qui nécessitent une intervention humaine.
- État final : READY_FOR_REVIEW, BLOCKED ou NEEDS_HUMAN.

Ne déclare pas une mission terminée si elle n'est pas vérifiée.
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
        "title": "BOT WORKER - " + title[:70],
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
            result = json.loads(
                response.read().decode("utf-8")
            )

        if not result.get("ok") or not result.get("task_id"):
            raise RuntimeError(str(result))

        return result

    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Manus HTTP {e.code}: {details[:500]}"
        )


def run():

    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY manquante.")

    db = load_db()

    active = [
        m for m in db.get("missions", [])
        if m.get("status") in (
            "IN_PROGRESS",
            "WAITING_HUMAN",
            "BLOCKED"
        )
    ]

    if active:
        log("Une mission est déjà active. Arrêt.")
        return

    approved = [
        m for m in db.get("missions", [])
        if m.get("status") == "APPROVED"
    ]

    if not approved:
        log("Aucune mission approuvée.")
        return

    # Une seule mission à la fois.
    mission = approved[0]

    log("Création de la tâche Manus...")
    log(mission.get("title", ""))

    result = create_manus_task(mission)

    mission["status"] = "IN_PROGRESS"

    mission["execution"] = {
        "status": "RUNNING",
        "executor": "MANUS",
        "task_id": result["task_id"],
        "task_url": result.get("task_url"),
        "started_at": now(),
        "result": None,
        "submitted": False
    }

    db["system"]["status"] = "IN_PROGRESS"
    db["system"]["current_mission"] = mission["id"]

    save_db(db)

    log("Tâche Manus créée.")
    log("Task ID : " + result["task_id"])
    log("URL : " + str(result.get("task_url")))


if __name__ == "__main__":
    run()