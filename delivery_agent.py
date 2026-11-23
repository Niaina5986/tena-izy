import os
import re
import json
import ast
import hashlib
import mimetypes
import requests

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse


# =========================================================
# CONFIGURATION
# =========================================================

ROOT = Path(__file__).resolve().parent

DB_FILE = ROOT / "db.json"
DELIVERY_DIR = ROOT / "deliveries"

MANUS_API = "https://api.manus.ai/v2"

MANUS_KEY = os.getenv("MANUS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

GH_PAT = os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN")

MAX_FILE_SIZE = 10 * 1024 * 1024
TIMEOUT = 60

HEADERS = {
    "x-manus-api-key": MANUS_KEY or "",
    "Content-Type": "application/json"
}


# =========================================================
# DATE
# =========================================================

def now():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# TELEGRAM
# =========================================================

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

    except requests.RequestException as error:

        print("Erreur Telegram :", error)

        return False


# =========================================================
# BASE DE DONNÉES
# =========================================================

def load_db():

    if not DB_FILE.exists():

        raise FileNotFoundError(
            "db.json introuvable."
        )

    with open(DB_FILE, "r", encoding="utf-8") as file:

        db = json.load(file)

    if not isinstance(db, dict):

        raise ValueError(
            "Format db.json invalide."
        )

    missions = db.get("missions", {})

    if isinstance(missions, list):

        converted = {}

        for index, mission in enumerate(missions):

            if not isinstance(mission, dict):
                continue

            mission_id = str(
                mission.get("id")
                or mission.get("mission_id")
                or f"legacy-{index}"
            )

            converted[mission_id] = mission

        missions = converted

    if not isinstance(missions, dict):

        raise ValueError(
            "Le champ missions de db.json est invalide."
        )

    db["missions"] = missions

    return db


def save_db(db):

    db["updated_at"] = now()

    temporary = DB_FILE.with_suffix(".json.tmp")

    with open(
        temporary,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            db,
            file,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temporary, DB_FILE)


# =========================================================
# API MANUS
# =========================================================

def manus_get(endpoint, params):

    if not MANUS_KEY:

        raise RuntimeError(
            "MANUS_API_KEY absent."
        )

    response = requests.get(
        MANUS_API + endpoint,
        headers=HEADERS,
        params=params,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    if data.get("ok") is False:

        raise RuntimeError(
            str(data.get("error", data))
        )

    return data


def get_manus_task(task_id):

    detail = manus_get(
        "/task.detail",
        {"task_id": task_id}
    )

    messages_data = manus_get(
        "/task.listMessages",
        {
            "task_id": task_id,
            "order": "asc",
            "limit": 200
        }
    )

    task = detail.get("task", detail)

    messages = messages_data.get(
        "messages",
        []
    )

    if not messages and isinstance(
        messages_data.get("data"), dict
    ):

        messages = messages_data["data"].get(
            "messages",
            []
        )

    return task, messages


# =========================================================
# EXTRACTION DES FICHIERS
# =========================================================

def collect_attachments(messages):

    attachments = {}

    if not isinstance(messages, list):
        return []

    for event in messages:

        if not isinstance(event, dict):
            continue

        assistant = event.get(
            "assistant_message",
            {}
        )

        if not isinstance(assistant, dict):
            continue

        files = assistant.get(
            "attachments",
            []
        )

        if not isinstance(files, list):
            continue

        for item in files:

            if not isinstance(item, dict):
                continue

            download_url = item.get("url")

            filename = item.get(
                "filename",
                "livrable"
            )

            if not download_url:
                continue

            if not str(download_url).startswith("https://"):
                continue

            file_uid = item.get("file_uid", "")
            version_uid = item.get("version_uid", "")

            unique_key = (
                str(file_uid)
                + ":"
                + str(version_uid)
            )

            if unique_key == ":":
                unique_key = (
                    str(filename)
                    + ":"
                    + str(download_url)
                )

            attachments[unique_key] = item

    return list(attachments.values())


# =========================================================
# SÉCURISATION DES NOMS
# =========================================================

def safe_name(filename):

    filename = Path(
        str(filename)
    ).name

    filename = re.sub(
        r"[^a-zA-Z0-9._-]",
        "_",
        filename
    )

    filename = filename.strip("._")

    if not filename:
        filename = "livrable.bin"

    return filename[:150]


def safe_mission_id(mission_id):

    return re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        str(mission_id)
    )[:100]


# =========================================================
# TÉLÉCHARGEMENT
# =========================================================

def download_file(url, destination):

    total = 0

    with requests.get(
        url,
        stream=True,
        timeout=TIMEOUT
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as file:

            for chunk in response.iter_content(
                chunk_size=8192
            ):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_FILE_SIZE:

                    raise ValueError(
                        "Fichier trop volumineux : limite 10 Mo."
                    )

                file.write(chunk)

    return total


# =========================================================
# VALIDATION DES FICHIERS
# =========================================================

def validate_file(path):

    extension = path.suffix.lower()

    result = {
        "valid": True,
        "type": extension or "unknown",
        "message": "Fichier téléchargé."
    }

    try:

        if extension == ".json":

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as file:

                json.load(file)

            result["message"] = (
                "JSON valide."
            )

        elif extension == ".py":

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as file:

                source = file.read()

            ast.parse(source)

            result["message"] = (
                "Syntaxe Python valide."
            )

        elif extension in (
            ".txt",
            ".md",
            ".html",
            ".css",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".xml",
            ".csv",
            ".yml",
            ".yaml",
            ".toml",
            ".sql"
        ):

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as file:

                file.read()

            result["message"] = (
                "Texte lisible en UTF-8."
            )

        else:

            result["message"] = (
                "Téléchargement réussi. "
                "Validation automatique non disponible."
            )

    except Exception as error:

        result["valid"] = False

        result["message"] = str(error)

    return result


# =========================================================
# TRAITEMENT D'UNE MISSION
# =========================================================

def process_mission(mission_id, mission):

    task_id = mission.get(
        "manus_task_id"
    )

    if not task_id:
        return False

    print(
        "Surveillance :",
        mission.get("title", mission_id)
    )

    task, messages = get_manus_task(
        task_id
    )

    status = str(
        task.get("status", "unknown")
    ).lower()

    background_jobs = task.get(
        "has_running_background_jobs",
        False
    )

    previous_status = mission.get(
        "delivery_status"
    )

    mission["manus_status"] = status
    mission["delivery_last_checked"] = now()

    # -----------------------------------------------------
    # TÂCHE EN COURS
    # -----------------------------------------------------

    if status in (
        "running",
        "waiting"
    ):

        print(
            "Manus travaille encore :",
            status
        )

        return True

    # -----------------------------------------------------
    # ERREUR MANUS
    # -----------------------------------------------------

    if status == "error":

        mission["delivery_status"] = "MANUS_ERROR"

        if previous_status != "MANUS_ERROR":

            notify(
                "❌ TENA IZY — ERREUR MANUS\n\n"
                f"Mission : {mission.get('title')}\n"
                f"Task ID : {task_id}"
            )

        return True

    # -----------------------------------------------------
    # BESOIN D'INTERVENTION
    # -----------------------------------------------------

    if status == "requires_input":

        mission["delivery_status"] = "HUMAN_REQUIRED"

        if previous_status != "HUMAN_REQUIRED":

            notify(
                "⚠️ TENA IZY — ACTION NÉCESSAIRE\n\n"
                f"Mission : {mission.get('title')}\n"
                "Manus attend une intervention."
            )

        return True

    # -----------------------------------------------------
    # ATTENDRE LA FIN DES TÂCHES SECONDAIRES
    # -----------------------------------------------------

    if status not in (
        "stopped",
        "completed"
    ):

        print(
            "Statut non final :",
            status
        )

        return True

    if background_jobs:

        mission["delivery_status"] = (
            "WAITING_BACKGROUND_JOBS"
        )

        print(
            "Manus possède encore des tâches secondaires."
        )

        return True

    # -----------------------------------------------------
    # RÉCUPÉRER LES PIÈCES JOINTES
    # -----------------------------------------------------

    attachments = collect_attachments(
        messages
    )

    if not attachments:

        mission["delivery_status"] = (
            "WAITING_FOR_ATTACHMENTS"
        )

        if previous_status != "WAITING_FOR_ATTACHMENTS":

            notify(
                "⏳ TENA IZY — LIVRABLE NON TROUVÉ\n\n"
                f"Mission : {mission.get('title')}\n"
                "Manus a terminé, mais aucun fichier "
                "joint n'a été trouvé.\n"
                "Nouvelle vérification automatique prévue."
            )

        return True

    # -----------------------------------------------------
    # DOSSIER DE LIVRAISON
    # -----------------------------------------------------

    folder = (
        DELIVERY_DIR
        / safe_mission_id(mission_id)
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    recorded_files = mission.get(
        "delivery_files",
        []
    )

    if not isinstance(recorded_files, list):
        recorded_files = []

    known_ids = {
        item.get("file_uid")
        for item in recorded_files
        if isinstance(item, dict)
    }

    all_valid = True
    downloaded_count = 0

    # -----------------------------------------------------
    # TRAITER CHAQUE FICHIER
    # -----------------------------------------------------

    for attachment in attachments:

        file_uid = attachment.get(
            "file_uid"
        )

        version_uid = attachment.get(
            "version_uid"
        )

        filename = safe_name(
            attachment.get(
                "filename",
                "livrable.bin"
            )
        )

        if file_uid and file_uid in known_ids:
            continue

        download_url = attachment.get(
            "url"
        )

        if not download_url:
            continue

        # Éviter les noms en double.

        destination = folder / filename

        if destination.exists():

            stem = destination.stem
            suffix = destination.suffix

            destination = folder / (
                stem
                + "_"
                + str(int(datetime.now().timestamp()))
                + suffix
            )

        try:

            size = download_file(
                download_url,
                destination
            )

            validation = validate_file(
                destination
            )

            with open(
                destination,
                "rb"
            ) as file:

                sha256 = hashlib.sha256(
                    file.read()
                ).hexdigest()

            file_record = {
                "filename": destination.name,
                "file_uid": file_uid,
                "version_uid": version_uid,
                "path": str(
                    destination.relative_to(ROOT)
                ),
                "size": size,
                "sha256": sha256,
                "validation": validation,
                "downloaded_at": now()
            }

            recorded_files.append(
                file_record
            )

            downloaded_count += 1

            if not validation["valid"]:
                all_valid = False

            print(
                "Fichier récupéré :",
                destination
            )

        except Exception as error:

            all_valid = False

            print(
                "Erreur fichier :",
                filename,
                error
            )

            notify(
                "⚠️ TENA IZY — ERREUR DE TÉLÉCHARGEMENT\n\n"
                f"Mission : {mission.get('title')}\n"
                f"Fichier : {filename}\n"
                f"Erreur : {str(error)[:500]}"
            )

    # -----------------------------------------------------
    # ENREGISTRER LES RÉSULTATS
    # -----------------------------------------------------

    mission["delivery_files"] = recorded_files

    if not recorded_files:

        mission["delivery_status"] = (
            "WAITING_FOR_ATTACHMENTS"
        )

        return True

    if not all_valid:

        mission["delivery_status"] = (
            "FILES_DOWNLOADED_NEEDS_REVIEW"
        )

        notify(
            "⚠️ TENA IZY — CONTRÔLE NÉCESSAIRE\n\n"
            f"Mission : {mission.get('title')}\n"
            f"Fichiers récupérés : {downloaded_count}\n"
            "Au moins un fichier nécessite une vérification."
        )

        return True

    # -----------------------------------------------------
    # ARCHIVAGE GITHUB EN ATTENTE DU COMMIT
    # -----------------------------------------------------

    mission["delivery_status"] = (
        "ARCHIVED_IN_GITHUB_PENDING_PUSH"
    )

    mission["delivery_prepared_at"] = now()

    notify(
        "📦 TENA IZY — FICHIERS RÉCUPÉRÉS\n\n"
        f"Mission : {mission.get('title')}\n"
        f"Fichiers : {len(recorded_files)}\n"
        f"Dossier : deliveries/{safe_mission_id(mission_id)}/\n\n"
        "Contrôle automatique terminé.\n"
        "Archivage GitHub en cours.\n"
        "La livraison à la plateforme externe "
        "n'est pas encore confirmée."
    )

    return True


# =========================================================
# PROGRAMME PRINCIPAL
# =========================================================

def main():

    print("=" * 55)
    print("TENA IZY — DELIVERY AGENT")
    print("=" * 55)

    if not MANUS_KEY:

        raise RuntimeError(
            "MANUS_API_KEY absent."
        )

    db = load_db()

    missions = db["missions"]

    processed = 0
    errors = 0

    for mission_id, mission in missions.items():

        if not isinstance(mission, dict):
            continue

        if not mission.get("manus_task_id"):
            continue

        try:

            process_mission(
                mission_id,
                mission
            )

            processed += 1

        except Exception as error:

            errors += 1

            mission["delivery_last_error"] = str(
                error
            )

            mission["delivery_last_checked"] = now()

            print(
                "Erreur mission :",
                mission_id,
                error
            )

    save_db(db)

    print("=" * 55)
    print