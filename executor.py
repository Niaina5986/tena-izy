import os
import json
import requests
from datetime import datetime, timezone

# =====================================================
# CONFIGURATION
# =====================================================

DB_FILE = "db.json"

MANUS_KEY = os.getenv("MANUS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

MAX_TASKS = int(os.getenv("MAX_MANUS_TASKS_PER_RUN", "1"))

MANUS_API = "https://api.manus.ai/v2"

TIMEOUT = 60


# =====================================================
# DATE
# =====================================================

def now():
    return datetime.now(timezone.utc).isoformat()


# =====================================================
# NORMALISATION DES MISSIONS
# =====================================================

def normalize_missions(missions):

    if isinstance(missions, dict):
        return missions

    converted = {}

    if isinstance(missions, list):

        for index, mission in enumerate(missions):

            if not isinstance(mission, dict):
                continue

            mission_id = (
                mission.get("id")
                or mission.get("mission_id")
                or mission.get("url")
                or f"legacy:{index}"
            )

            converted[str(mission_id)] = mission

    return converted


# =====================================================
# BASE DE DONNEES
# =====================================================

def load_db():

    if not os.path.exists(DB_FILE):
        return {"missions": {}, "history": []}

    try:

        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        # Ancien format : le fichier entier est une liste
        if isinstance(db, list):
            db = {"missions": db, "history": []}

        if not isinstance(db, dict):
            db = {"missions": {}, "history": []}

        missions = db.get("missions", {})

        db["missions"] = normalize_missions(missions)

        db.setdefault("history", [])

        return db

    except json.JSONDecodeError as e:

        raise RuntimeError(
            f"db.json contient une erreur JSON : {e}"
        )


def save_db(db):

    db["updated_at"] = now()

    temp_file = DB_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:

        json.dump(
            db,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temp_file, DB_FILE)


# =====================================================
# TELEGRAM
# =====================================================

def notify(message):

    if not TG_TOKEN or not TG_CHAT:

        print("[TELEGRAM]", message)
        return False

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",

            json={
                "chat_id": TG_CHAT,
                "text": message,
                "disable_web_page_preview": True
            },

            timeout=20
        )

        response.raise_for_status()

        return True

    except requests.RequestException as e:

        print("Erreur Telegram :", e)

        return False


# =====================================================
# API MANUS
# =====================================================

def manus_request(method, endpoint, **kwargs):

    if not MANUS_KEY:
        raise RuntimeError("MANUS_API_KEY manquant")

    headers = {
        "x-manus-api-key": MANUS_KEY,
        "Content-Type": "application/json"
    }

    response = requests.request(

        method=method,

        url=MANUS_API + endpoint,

        headers=headers,

        timeout=TIMEOUT,

        **kwargs
    )

    response.raise_for_status()

    data = response.json()

    if data.get("ok") is False:

        raise RuntimeError(
            str(data.get("error", "Erreur API Manus"))
        )

    return data


# =====================================================
# EXTRACTION DU RESULTAT MANUS
# =====================================================

def extract_output(messages):

    output = []

    if not isinstance(messages, list):
        return None

    for event in messages:

        if not isinstance(event, dict):
            continue

        structured = event.get(
            "structured_output_result"
        )

        if isinstance(structured, dict):

            if structured.get("success"):

                value = structured.get("value")

                if value:
                    output.append(str(value))

        assistant = event.get(
            "assistant_message", {}
        )

        if isinstance(assistant, dict):

            content = assistant.get("content")

            if isinstance(content, str):

                output.append(content)

            elif isinstance(content, list):

                for item in content:

                    if isinstance(item, dict):

                        text = item.get("text")

                        if text:
                            output.append(str(text))

        # Certaines réponses contiennent directement du texte.
        text = event.get("text")

        if isinstance(text, str):
            output.append(text)

    if not output:
        return None

    return "\n\n".join(dict.fromkeys(output))


# =====================================================
# CREATION D'UNE TACHE MANUS
# =====================================================

def create_task(mission):

    title = mission.get(
        "title",
        "Mission sans titre"
    )

    description = mission.get(
        "description",
        ""
    )

    url = mission.get(
        "url",
        ""
    )

    reward = mission.get(
        "reward",
        mission.get("reward_usd", 0)
    )

    currency = mission.get(
        "currency",
        "USD"
    )

    platform = mission.get(
        "source_board",
        mission.get("source", "Inconnue")
    )

    prompt = f"""
Tu es l'agent d'exécution de TENA IZY.

MISSION :
{title}

DESCRIPTION :
{description}

LIEN ORIGINAL :
{url}

RÉCOMPENSE ANNONCÉE :
{reward} {currency}

PLATEFORME :
{platform}

OBJECTIFS :

1. Examiner les conditions de la mission.
2. Vérifier qu'elle est encore ouverte.
3. Vérifier que les agents IA sont autorisés.
4. Vérifier les conditions de paiement.
5. Vérifier l'accessibilité depuis Madagascar.
6. Déterminer si le travail est réalisable.
7. Réaliser le travail si les conditions sont réunies.
8. Produire les fichiers nécessaires.
9. Effectuer les tests.
10. Préparer les instructions de livraison.

RÈGLES :

- Ne jamais inventer une récompense.
- Ne jamais inventer une acceptation.
- Ne jamais inventer un paiement.
- Ne jamais payer pour accéder à une mission.
- Ne jamais contourner CAPTCHA ou KYC.
- Ne jamais utiliser de faux comptes.
- Ne jamais divulguer de secrets.
- Ne jamais effectuer de transaction financière.
- Respecter les règles de la plateforme.
- Ne pas soumettre un travail sans vérifier les conditions.
- Si une action humaine obligatoire est nécessaire,
  signaler HUMAN_REQUIRED.

LIVRABLE ATTENDU :

DECISION:
REWARD:
ELIGIBILITY:
PAYMENT_METHOD:
WORK_COMPLETED:
FILES_CREATED:
TESTS:
DELIVERY_URL:
BLOCKERS:

Ne prétends jamais avoir livré un travail
si la livraison n'a pas réellement été effectuée.
"""

    data = manus_request(

        "POST",

        "/task.create",

        json={

            "title": "TENA IZY - " + title[:100],

            "agent_profile": "lite",

            "interactive_mode": False,

            "message": {

                "content": [

                    {

                        "type": "text",

                        "text": prompt,

                        "visibility": "visible"

                    }

                ]

            }

        }
    )

    task_id = data.get("task_id")

    if not task_id:
        raise RuntimeError(
            "Manus n'a pas retourné de task_id : "
            + str(data)
        )

    return data


# =====================================================
# SUIVI MANUS
# =====================================================

def get_task_status(task_id):

    detail = manus_request(

        "GET",

        "/task.detail",

        params={
            "task_id": task_id
        }
    )

    messages_data = manus_request(

        "GET",

        "/task.listMessages",

        params={

            "task_id": task_id,

            "order": "desc",

            "limit": 30
        }
    )

    task = detail.get("task", {})

    messages = messages_data.get(
        "messages", []
    )

    status = task.get(
        "status",
        "unknown"
    )

    output = extract_output(messages)

    background_jobs = task.get(
        "has_running_background_jobs",
        False
    )

    return {

        "status": status,

        "output": output,

        "background_jobs": background_jobs,

        "task": task
    }


# =====================================================
# SURVEILLANCE DES MISSIONS
# =====================================================

def monitor_missions(missions):

    for mission_id, mission in missions.items():

        if not isinstance(mission, dict):
            continue

        task_id = mission.get("manus_task_id")

        if not task_id:
            continue

        try:

            result = get_task_status(task_id)

            previous = mission.get(
                "manus_status"
            )

            current = result["status"]

            mission["manus_status"] = current

            mission["last_checked"] = now()

            if result["output"]:

                mission["last_result"] = result["output"]

            # Changement de statut
            if current != previous:

                notify(

                    "🤖 TENA IZY — PROGRESSION\n\n"

                    f"📌 {mission.get('title', 'Mission')}\n"

                    f"État Manus : {current}\n"

                    f"Récompense annoncée : "
                    f"{mission.get('reward', 0)} "
                    f"{mission.get('currency', 'USD')}"

                )

            # Travail terminé
            if current in ["completed", "stopped"]:

                if not result["background_jobs"]:

                    if mission.get("status") != "READY_FOR_DELIVERY_REVIEW":

                        mission["status"] = (
                            "READY_FOR_DELIVERY_REVIEW"
                        )

                        mission["completed_at"] = now()

                        notify(

                            "📦 TENA IZY — TRAVAIL TERMINÉ\n\n"

                            f"📌 {mission.get('title')}\n"

                            "Manus a terminé son exécution.\n"

                            "Le livrable doit être contrôlé "
                            "avant sa livraison externe."

                        )

            # Intervention humaine
            elif current in ["waiting", "requires_input"]:

                mission["status"] = "HUMAN_REQUIRED"

                if previous != current:

                    notify(

                        "⚠️ TENA IZY — INTERVENTION HUMAINE\n\n"

                        f"📌 {mission.get('title')}\n"

                        "Une action humaine est nécessaire."

                    )

            # Erreur
            elif current == "error":

                mission["status"] = "FAILED"

                notify(

                    "❌ TENA IZY — ÉCHEC\n\n"

                    f"📌 {mission.get('title')}\n"

                    "L'exécution Manus a échoué."

                )

        except Exception as e:

            mission["last_error"] = str(e)

            print(
                f"Erreur mission {mission_id} : {e}"
            )


# =====================================================
# DEMARRAGE DES MISSIONS
# =====================================================

def start_missions(missions):

    if not MANUS_KEY:

        print("MANUS_API_KEY absent.")
        return

    started = 0

    for mission_id, mission in missions.items():

        if started >= MAX_TASKS:
            break

        if not isinstance(mission, dict):
            continue

        # Seulement les nouvelles missions
        if mission.get("status") != "DISCOVERED":
            continue

        # Eviter les doublons
        if mission.get("manus_task_id"):
            continue

        try:

            result = create_task(mission)

            mission["manus_task_id"] = result["task_id"]

            mission["manus_status"] = "running"

            mission["status"] = "IN_PROGRESS"

            mission["started_at"] = now()

            started += 1

            notify(

                "🚀 TENA IZY — EXÉCUTION DÉMARRÉE\n\n"

                f"📌 {mission.get('title')}\n"

                f"💰 {mission.get('reward', 0)} "
                f"{mission.get('currency', 'USD')}\n\n"

                f"🤖 Tâche Manus : {result['task_id']}\n"

                f"🔗 {mission.get('url', '')}"

            )

        except Exception as e:

            mission["status"] = "EXECUTION_ERROR"

            mission["error"] = str(e)

            notify(

                "❌ TENA IZY — ERREUR DE DÉMARRAGE\n\n"

                f"📌 {mission.get('title')}\n"

                f"Erreur : {str(e)[:1000]}"

            )

    print("Missions démarrées :", started)


# =====================================================
# PROGRAMME PRINCIPAL
# =====================================================

def main():

    print("=" * 45)

    print("TENA IZY — EXECUTION V2")

    print("=" * 45)

    # Charger la base
    db = load_db()

    missions = db["missions"]

    print("Nombre de missions :", len(missions))

    print("Surveillance des tâches Manus...")

    # Surveiller les tâches existantes
    monitor_missions(missions)

    # Démarrer les nouvelles tâches
    start_missions(missions)

    # Enregistrer les modifications
    save_db(db)

    print("=" * 45)

    print("EXECUTION TERMINEE")

    print("Missions :", len(missions))

    print("=" * 45)


# =====================================================
# DEMARRAGE
# =====================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print("ERREUR FATALE :", str(e))

        notify(
            "🚨 TENA IZY — ERREUR FATALE\n\n"
            + str(e)[:1000]
        )

        raise