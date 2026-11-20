import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MANUS_API = "https://api.manus.ai/v2"
TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
)

TIMEOUT = 40

ACTIVE_EXECUTION_STATUSES = {
    "RUNNING",
    "WAITING",
    "NEEDS_INPUT",
    "MONITOR_ERROR"
}


# ============================================================
# OUTILS
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print("[MANUS MONITOR]", message)


def load_db():
    if not os.path.exists(DB_FILE):
        raise FileNotFoundError("db.json introuvable.")

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    if not isinstance(db.get("missions"), list):
        raise RuntimeError("Format db.json invalide : missions.")

    if not isinstance(db.get("system"), dict):
        db["system"] = {}

    return db


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


# ============================================================
# REQUÊTES HTTP
# ============================================================

def manus_request(endpoint, payload):
    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY manquante.")

    url = f"{MANUS_API}/{endpoint}"

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-manus-api-key": MANUS_API_KEY
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=TIMEOUT
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if isinstance(result, dict):
            if result.get("ok") is False:
                raise RuntimeError(str(result))

        return result

    except urllib.error.HTTPError as e:
        details = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Manus HTTP {e.code}: {details[:700]}"
        )

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Erreur réseau Manus : {e}"
        )


def get_task_detail(task_id):
    return manus_request(
        "task.detail",
        {"task_id": task_id}
    )


def get_task_messages(task_id):
    return manus_request(
        "task.listMessages",
        {
            "task_id": task_id,
            "limit": 100
        }
    )


# ============================================================
# EXTRACTION DES DONNÉES MANUS
# ============================================================

def find_value(data, keys):
    """
    Recherche une valeur dans une réponse JSON,
    même si Manus ajoute des niveaux data/result.
    """

    if isinstance(data, dict):

        for key in keys:
            if key in data:
                return data[key]

        for value in data.values():
            result = find_value(value, keys)

            if result is not None:
                return result

    elif isinstance(data, list):

        for item in data:
            result = find_value(item, keys)

            if result is not None:
                return result

    return None


def extract_status(detail):
    status = find_value(
        detail,
        ["status", "agent_status"]
    )

    if not status:
        return "UNKNOWN"

    return str(status).lower()


def extract_background_jobs(detail):
    value = find_value(
        detail,
        ["has_running_background_jobs"]
    )

    return value is True


def extract_messages(response):
    messages = find_value(
        response,
        ["messages", "items"]
    )

    if isinstance(messages, list):
        return messages

    return []


def extract_assistant_result(response):
    """
    Récupère les réponses de l'agent Manus.
    """

    messages = extract_messages(response)

    results = []

    for message in messages:

        if not isinstance(message, dict):
            continue

        message_type = str(
            message.get("type", "")
        ).lower()

        role = str(
            message.get("role", "")
        ).lower()

        if (
            message_type == "assistant_message"
            or role == "assistant"
        ):

            content = message.get("content")

            if isinstance(content, str):
                results.append(content)

            elif isinstance(content, list):

                for item in content:

                    if not isinstance(item, dict):
                        continue

                    if item.get("type") == "text":
                        text = item.get("text")

                        if text:
                            results.append(text)

    return "\n\n".join(results).strip()


def extract_waiting_information(detail, messages):
    """
    Recherche la raison pour laquelle Manus attend.
    """

    description = find_value(
        detail,
        [
            "waiting_description",
            "description"
        ]
    )

    event_type = find_value(
        detail,
        ["waiting_for_event_type"]
    )

    event_id = find_value(
        detail,
        ["waiting_for_event_id"]
    )

    if not description:
        description = "Manus attend une intervention."

    return {
        "description": str(description),
        "event_type": event_type,
        "event_id": event_id
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram non configuré.")
        return False

    url = f"{TELEGRAM_API}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }

    data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("ok"):
            return True

        log("Telegram refuse le message : " + str(result))
        return False

    except Exception as e:
        log("Erreur Telegram : " + str(e))
        return False


def notify_once(mission, notification_key, message):

    execution = mission.setdefault(
        "execution",
        {}
    )

    sent = execution.setdefault(
        "notifications_sent",
        []
    )

    if notification_key in sent:
        return

    if telegram_send(message):

        sent.append(notification_key)
        execution["last_notification_at"] = now()


# ============================================================
# TRAITEMENT DES MISSIONS
# ============================================================

def process_mission(mission):

    execution = mission.get("execution", {})

    task_id = execution.get("task_id")

    if not task_id:
        return False

    current_status = execution.get("status", "")

    if current_status not in ACTIVE_EXECUTION_STATUSES:
        return False

    title = mission.get(
        "title",
        "Mission sans titre"
    )

    log("Surveillance : " + title[:90])

    try:

        detail = get_task_detail(task_id)

        status = extract_status(detail)

        execution["last_checked_at"] = now()
        execution["last_api_status"] = status

        # ----------------------------------------------------
        # 1. MANUS TRAVAILLE
        # ----------------------------------------------------

        if status == "running":

            mission["status"] = "IN_PROGRESS"
            execution["status"] = "RUNNING"

            log("Manus travaille encore.")

            return True

        # ----------------------------------------------------
        # 2. MANUS ATTEND UNE ACTION
        # ----------------------------------------------------

        if status == "waiting":

            messages = get_task_messages(task_id)

            waiting = extract_waiting_information(
                detail,
                messages
            )

            mission["status"] = "WAITING_HUMAN"
            execution["status"] = "NEEDS_INPUT"

            execution["waiting"] = waiting

            notification_key = (
                "WAITING_"
                + str(waiting.get("event_id") or "UNKNOWN")
            )

            message = (
                "⚠️ BOT WORKER : INTERVENTION NÉCESSAIRE\n\n"
                f"Mission : {title}\n\n"
                f"Raison : {waiting['description']}\n\n"
                f"Task ID : {task_id}\n"
                f"Manus : {execution.get('task_url', '')}\n\n"
                "Aucune action sensible ne sera confirmée "
                "automatiquement."
            )

            notify_once(
                mission,
                notification_key,
                message
            )

            log("Manus attend une intervention humaine.")

            return True

        # ----------------------------------------------------
        # 3. MANUS EST ARRÊTÉ
        # ----------------------------------------------------

        if status == "stopped":

            # Un agent peut être arrêté alors que
            # des tâches secondaires continuent.

            if extract_background_jobs(detail):

                mission["status"] = "IN_PROGRESS"
                execution["status"] = "RUNNING"

                log(
                    "Agent arrêté, mais tâches secondaires actives."
                )

                return True

            messages = get_task_messages(task_id)

            result = extract_assistant_result(messages)

            execution["result"] = result
            execution["finished_at"] = now()
            execution["status"] = "READY_FOR_REVIEW"

            mission["status"] = "WAITING_HUMAN"

            db_result = (
                "MISSION TERMINÉE PAR MANUS\n\n"
                f"Mission : {title}\n\n"
                "Résultat disponible pour vérification.\n\n"
                f"Résumé :\n{result[:3000]}\n\n"
                f"Manus : {execution.get('task_url', '')}\n\n"
                "Vérifie le résultat avant de valider "
                "la mission ou de la soumettre."
            )

            notify_once(
                mission,
                "READY_FOR_REVIEW",
                db_result
            )

            log("Résultat Manus prêt pour vérification.")

            return True

        # ----------------------------------------------------
        # 4. ERREUR MANUS
        # ----------------------------------------------------

        if status == "error":

            error_message = find_value(
                detail,
                [
                    "error",
                    "error_message",
                    "message"
                ]
            )

            if not error_message:
                error_message = "Erreur inconnue de Manus."

            execution["status"] = "FAILED"
            execution["error"] = str(error_message)
            execution["failed_at"] = now()

            mission["status"] = "BLOCKED"

            notify_once(
                mission,
                "MANUS_ERROR",
                (
                    "❌ BOT WORKER : ERREUR MANUS\n\n"
                    f"Mission : {title}\n\n"
                    f"Erreur : {str(error_message)[:2000]}\n\n"
                    f"Task ID : {task_id}"
                )
            )

            log("Mission bloquée à cause d'une erreur.")

            return True

        # ----------------------------------------------------
        # 5. STATUT INCONNU
        # ----------------------------------------------------

        log("Statut Manus non reconnu : " + status)

        execution["last_unknown_status"] = status

        return True

    except Exception as e:

        log("Erreur de surveillance : " + str(e))

        execution["monitor_error"] = str(e)
        execution["last_monitor_error_at"] = now()

        # Ne pas remplacer RUNNING par un statut définitif.
        # La prochaine exécution réessaiera.

        if execution.get("status") != "NEEDS_INPUT":
            execution["status"] = "MONITOR_ERROR"

        notify_once(
            mission,
            "MONITOR_ERROR",
            (
                "⚠️ BOT WORKER : ERREUR DE SURVEILLANCE\n\n"
                f"Mission : {title}\n\n"
                f"Erreur : {str(e)[:1500]}\n\n"
                "Le système réessaiera automatiquement."
            )
        )

        return True


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def run():

    log("Démarrage du moniteur Manus.")

    if not MANUS_API_KEY:
        raise RuntimeError(
            "Le secret MANUS_API_KEY est manquant."
        )

    db = load_db()

    missions = db.get("missions", [])

    processed = 0

    for mission in missions:

        execution = mission.get("execution", {})

        if execution.get("executor") != "MANUS":
            continue

        if not execution.get("task_id"):
            continue

        if execution.get("status") not in ACTIVE_EXECUTION_STATUSES:
            continue

        changed = process_mission(mission)

        if changed:
            processed += 1

        # Petite pause pour éviter les requêtes trop rapprochées.
        time.sleep(1)

    if processed > 0:
        save_db(db)

    log(f"{processed} mission(s) surveillée(s).")
    log("Surveillance terminée.")


if __name__ == "__main__":
    run()