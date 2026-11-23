import os
import re
import json
import ast
import hashlib
import requests

from pathlib import Path
from datetime import datetime, timezone


# =========================================================
# CONFIGURATION
# =========================================================

ROOT = Path(__file__).resolve().parent

DB_FILE = ROOT / "db.json"
DELIVERY_DIR = ROOT / "deliveries"

MANUS_API = "https://api.manus.ai/v2"

MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_FILE_SIZE = 10 * 1024 * 1024
TIMEOUT = 60

HEADERS = {
    "x-api-key": MANUS_API_KEY,
    "Content-Type": "application/json"
}

SESSION = requests.Session()


# =========================================================
# OUTILS
# =========================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def notify(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configuré.")
        return

    try:
        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        response = SESSION.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=TIMEOUT
        )

        response.raise_for_status()

    except Exception as exc:
        print(f"Erreur Telegram : {exc}")


def load_db():
    if not DB_FILE.exists():
        raise FileNotFoundError("db.json introuvable.")

    with open(DB_FILE, "r", encoding="utf-8") as file:
        db = json.load(file)

    if not isinstance(db, dict):
        raise ValueError("Format db.json invalide.")

    missions = db.get("missions", {})

    if isinstance(missions, list):
        converted = {}

        for mission in missions:
            if isinstance(mission, dict):
                mission_id = str(mission.get("id", ""))

                if mission_id:
                    converted[mission_id] = mission

        db["missions"] = converted

    elif not isinstance(missions, dict):
        raise ValueError("Le champ missions est invalide.")

    return db


def save_db(db):
    db["updated_at"] = now()

    temporary = DB_FILE.with_suffix(".tmp")

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(
            db,
            file,
            indent=2,
            ensure_ascii=False
        )

    temporary.replace(DB_FILE)


# =========================================================
# API MANUS
# =========================================================

def manus_get(endpoint, params=None):
    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY absent.")

    url = f"{MANUS_API}{endpoint}"

    response = SESSION.get(
        url,
        headers=HEADERS,
        params=params,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return response.json()


def get_manus_task(task_id):
    detail = manus_get(
        "/task.detail",
        {"task_id": task_id}
    )

    messages = manus_get(
        "/task.listMessages",
        {
            "task_id": task_id,
            "order": "asc",
            "limit": 200
        }
    )

    task = detail.get("task", detail)

    if isinstance(messages, dict):
        message_list = messages.get("messages", [])

        if not message_list:
            data = messages.get("data", {})
            message_list = data.get("messages", [])

    else:
        message_list = []

    if not isinstance(message_list, list):
        message_list = []

    return task, message_list


# =========================================================
# EXTRACTION DES FICHIERS MANUS
# =========================================================

def collect_attachments(messages):
    attachments = []
    seen = set()

    for message in messages:

        if not isinstance(message, dict):
            continue

        assistant_message = message.get(
            "assistant_message",
            message
        )

        if not isinstance(assistant_message, dict):
            continue

        files = assistant_message.get("attachments", [])

        if not isinstance(files, list):
            continue

        for item in files:

            if not isinstance(item, dict):
                continue

            url = item.get("url")

            if not url or not url.startswith("https://"):
                continue

            file_uid = item.get("file_uid")
            version_uid = item.get("version_uid")

            identity = (
                str(file_uid or ""),
                str(version_uid or ""),
                str(item.get("filename", ""))
            )

            if identity in seen:
                continue

            seen.add(identity)

            attachments.append({
                "url": url,
                "filename": item.get("filename", "fichier"),
                "file_uid": file_uid,
                "version_uid": version_uid,
                "content_type": item.get("content_type")
            })

    return attachments


# =========================================================
# SÉCURITÉ DES NOMS DE FICHIERS
# =========================================================

def safe_name(value):
    value = str(value or "fichier")

    value = value.replace("\\", "/")
    value = value.split("/")[-1]

    value = re.sub(
        r"[^a-zA-Z0-9._-]",
        "_",
        value
    )

    return value[:120] or "fichier"


def safe_mission_id(value):
    return re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        str(value)
    )


# =========================================================
# TÉLÉCHARGEMENT
# =========================================================

def download_file(url, destination):
    total = 0

    destination.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with SESSION.get(
        url,
        stream=True,
        timeout=TIMEOUT
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as file:

            for chunk in response.iter_content(8192):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_FILE_SIZE:
                    raise ValueError(
                        "Fichier trop volumineux : limite 10 MB."
                    )

                file.write(chunk)

    if total == 0:
        raise ValueError("Fichier vide.")

    return total


# =========================================================
# VALIDATION DES FICHIERS
# =========================================================

def validate_file(path):
    extension = path.suffix.lower()

    try:

        if extension == ".json":

            with open(path, "r", encoding="utf-8") as file:
                json.load(file)

            return True, "JSON valide"

        if extension == ".py":

            source = path.read_text(
                encoding="utf-8"
            )

            ast.parse(source)

            return True, "Syntaxe Python valide"

        if extension in (
            ".txt",
            ".md",
            ".csv",
            ".html",
            ".css",
            ".js",
            ".xml",
            ".yaml",
            ".yml"
        ):

            path.read_text(encoding="utf-8")

            return True, "Fichier texte lisible"

        if extension in (".zip", ".pdf", ".png", ".jpg", ".jpeg"):

            if path.stat().st_size == 0:
                return False, "Fichier vide"

            return True, "Fichier téléchargé, contenu non vérifié"

        if path.stat().st_size == 0:
            return False, "Fichier vide"

        return True, "Fichier téléchargé, format non vérifié"

    except Exception as exc:
        return False, str(exc)


def sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file:

        for chunk in iter(
            lambda: file.read(8192),
            b""
        ):
            digest.update(chunk)

    return digest.hexdigest()


# =========================================================
# TRAITEMENT D'UNE MISSION
# =========================================================

def process_mission(mission_id, mission):

    task_id = mission.get("manus_task_id")

    if not task_id:
        return False

    print(f"Vérification mission : {mission_id}")

    previous_status = mission.get("delivery_status")

    try:
        task, messages = get_manus_task(task_id)

    except Exception as exc:

        print(f"Erreur API Manus : {exc}")

        mission["delivery_last_error"] = str(exc)
        mission["delivery_last_checked"] = now()

        return True

    status = str(task.get("status", "")).lower()

    background_jobs = task.get(
        "has_running_background_jobs",
        False
    )

    mission["manus_status"] = status
    mission["delivery_last_checked"] = now()

    # -----------------------------------------------------
    # TÂCHE EN COURS
    # -----------------------------------------------------

    if status in ("running", "waiting") or background_jobs:

        mission["delivery_status"] = "MANUS_RUNNING"

        return True

    # -----------------------------------------------------
    # ERREUR MANUS
    # -----------------------------------------------------

    if status == "error":

        mission["delivery_status"] = "MANUS_ERROR"

        if previous_status != "MANUS_ERROR":

            notify(
                f"❌ TENA IZY\n"
                f"Erreur Manus pour la mission {mission_id}."
            )

        return True

    # -----------------------------------------------------
    # INTERVENTION HUMAINE
    # -----------------------------------------------------

    if status == "requires_input":

        mission["delivery_status"] = "HUMAN_REQUIRED"

        if previous_status != "HUMAN_REQUIRED":

            notify(
                f"⚠️ TENA IZY\n"
                f"Manus demande une intervention humaine.\n"
                f"Mission : {mission_id}"
            )

        return True

    # -----------------------------------------------------
    # ATTENDRE LA FIN RÉELLE
    # -----------------------------------------------------

    if status not in ("completed", "stopped"):

        mission["delivery_status"] = "WAITING_FOR_COMPLETION"

        return True

    if background_jobs:

        mission["delivery_status"] = "MANUS_BACKGROUND_RUNNING"

        return True

    # -----------------------------------------------------
    # RECHERCHER LES PIÈCES JOINTES
    # -----------------------------------------------------

    attachments = collect_attachments(messages)

    if not attachments:

        mission["delivery_status"] = "WAITING_FOR_ATTACHMENTS"

        if previous_status != "WAITING_FOR_ATTACHMENTS":

            notify(
                f"⚠️ TENA IZY\n"
                f"Manus a terminé, mais aucun fichier "
                f"récupérable n'a été trouvé.\n"
                f"Mission : {mission_id}"
            )

        return True

    # -----------------------------------------------------
    # DOSSIER DE LIVRAISON
    # -----------------------------------------------------

    folder = DELIVERY_DIR / safe_mission_id(mission_id)

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    records = mission.setdefault(
        "delivery_files",
        []
    )

    known = set()

    for record in records:

        if not isinstance(record, dict):
            continue

        known.add((
            str(record.get("file_uid") or ""),
            str(record.get("version_uid") or ""),
            str(record.get("filename") or "")
        ))

    new_files = 0
    errors = []

    # -----------------------------------------------------
    # TÉLÉCHARGER ET CONTRÔLER
    # -----------------------------------------------------

    for attachment in attachments:

        identity = (
            str(attachment.get("file_uid") or ""),
            str(attachment.get("version_uid") or ""),
            str(attachment.get("filename") or "")
        )

        if identity in known:
            continue

        filename = safe_name(
            attachment.get("filename")
        )

        destination = folder / filename

        if destination.exists():
            destination = folder / (
                f"{destination.stem}_"
                f"{len(records) + 1}"
                f"{destination.suffix}"
            )

        try:

            size = download_file(
                attachment["url"],
                destination
            )

            valid, validation_message = validate_file(
                destination
            )

            checksum = sha256_file(destination)

            record = {
                "filename": destination.name,
                "file_uid": attachment.get("file_uid"),
                "version_uid": attachment.get("version_uid"),
                "path": str(
                    destination.relative_to(ROOT)
                ),
                "size": size,
                "sha256": checksum,
                "valid": valid,
                "validation": validation_message,
                "downloaded_at": now()
            }

            records.append(record)

            known.add(identity)
            new_files += 1

            if not valid:
                errors.append(
                    f"{filename} : {validation_message}"
                )

            print(
                f"Fichier récupéré : {filename} "
                f"({size} octets)"
            )

        except Exception as exc:

            errors.append(
                f"{filename} : {exc}"
            )

            print(
                f"Erreur téléchargement {filename} : {exc}"
            )

    # -----------------------------------------------------
    # RÉSULTAT
    # -----------------------------------------------------

    if errors:

        mission["delivery_status"] = "FILES_NEED_REVIEW"
        mission["delivery_errors"] = errors

        if previous_status != "FILES_NEED_REVIEW":

            notify(
                f"⚠️ TENA IZY\n"
                f"Certains fichiers nécessitent une vérification.\n"
                f"Mission : {mission_id}\n"
                + "\n".join(errors)
            )

        return True

    if not records:

        mission["delivery_status"] = "WAITING_FOR_ATTACHMENTS"

        return True

    mission["delivery_status"] = "ARCHIVED_IN_GITHUB_PENDING_PUSH"
    mission["delivery_files_count"] = len(records)
    mission["delivery_updated_at"] = now()

    if previous_status != "ARCHIVED_IN_GITHUB_PENDING_PUSH":

        notify(
            f"📦 TENA IZY\n"
            f"Fichiers récupérés et contrôlés.\n"
            f"Mission : {mission_id}\n"
            f"Nouveaux fichiers : {new_files}\n"
            f"Total : {len(records)}\n"
            f"Archivage GitHub en attente."
        )

    return True


# =========================================================
# PROGRAMME PRINCIPAL
# =========================================================

def main():

    print("===================================")
    print("TENA IZY - DELIVERY AGENT")
    print("===================================")

    if not MANUS_API_KEY:
        raise RuntimeError(
            "La variable MANUS_API_KEY est absente."
        )

    DELIVERY_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    db = load_db()

    missions = db.get("missions", {})

    processed = 0

    for mission_id, mission in missions.items():

        if not isinstance(mission, dict):
            continue

        if not mission.get("manus_task_id"):
            continue

        try:

            changed = process_mission(
                mission_id,
                mission
            )

            if changed:
                processed += 1

        except Exception as exc:

            print(
                f"Erreur mission {mission_id} : {exc}"
            )

            mission["delivery_last_error"] = str(exc)
            mission["delivery_last_checked"] = now()

    save_db(db)

    print("-----------------------------------")
    print(f"Missions vérifiées : {processed}")
    print("Delivery Agent terminé.")
    print("-----------------------------------")


if __name__ == "__main__":
    main()