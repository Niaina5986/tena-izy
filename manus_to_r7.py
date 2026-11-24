import os
import re
import json
import time
import logging
import hashlib
import shutil
import requests

from pathlib import Path
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "db.json"
INBOX = ROOT / "delivery_inbox"

MANUS_URL = "https://api.manus.ai/v2"
MANUS_API_KEY = os.getenv("MANUS_API_KEY")

REPOSITORY = os.getenv(
    "DELIVERY_REPOSITORY",
    "Niaina5986/tena-izy"
)

BASE_BRANCH = os.getenv(
    "DELIVERY_BASE_BRANCH",
    "main"
)

TIMEOUT = 45
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TOTAL_SIZE = 40 * 1024 * 1024
MAX_MESSAGES = 50

CANDIDATE_STATUSES = {
    "EXECUTING",
    "RUNNING",
    "IN_PROGRESS",
    "READY_TO_DELIVER",
    "READY_FOR_DELIVERY_REVIEW"
}

BLOCKED_WORDS = [
    "NEEDS_HUMAN",
    "HUMAN_REQUIRED",
    "ACTION HUMAINE REQUISE",
    "BLOCKED",
    "MISSION BLOQUÉE",
    "MISSION BLOQUEE"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MANUS-R7] %(levelname)s: %(message)s"
)


# ============================================================
# SESSION HTTP AVEC RETRIES
# ============================================================

def create_session():

    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)

    return session


SESSION = create_session()


# ============================================================
# BASE DE DONNÉES
# ============================================================

def load_missions():

    if not DB_FILE.exists():
        raise RuntimeError("db.json introuvable")

    try:
        data = json.loads(
            DB_FILE.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise RuntimeError(
            f"Lecture db.json impossible : {error}"
        )

    missions = data.get("missions")

    if not isinstance(missions, dict):
        raise RuntimeError(
            "Structure db.json invalide : missions doit être un objet"
        )

    return missions


# ============================================================
# NORMALISATION DES MESSAGES MANUS
# ============================================================

def extract_assistant_message(message):

    if not isinstance(message, dict):
        return None

    assistant = message.get("assistant_message")

    if isinstance(assistant, dict):
        return assistant

    return None


def extract_message_text(assistant):

    content = assistant.get("content", "")

    if isinstance(content, str):
        return content

    if isinstance(content, list):

        parts = []

        for item in content:

            if isinstance(item, str):
                parts.append(item)

            elif isinstance(item, dict):

                text = (
                    item.get("text")
                    or item.get("content")
                    or ""
                )

                if text:
                    parts.append(str(text))

        return "\n".join(parts)

    return str(content or "")


def extract_attachments(assistant):

    attachments = assistant.get("attachments") or []

    if isinstance(attachments, dict):
        attachments = [attachments]

    if not isinstance(attachments, list):
        return []

    return [
        item for item in attachments
        if isinstance(item, dict)
    ]


# ============================================================
# RÉCUPÉRATION DES MESSAGES MANUS
# ============================================================

def get_manus_result(task_id):

    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY absent")

    response = SESSION.get(
        MANUS_URL + "/task.listMessages",
        headers={
            "x-manus-api-key": MANUS_API_KEY
        },
        params={
            "task_id": task_id,
            "order": "desc",
            "limit": MAX_MESSAGES
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    messages = data.get("messages", [])

    if not isinstance(messages, list):
        raise RuntimeError(
            "Format de réponse Manus invalide"
        )

    # Le premier message assistant trouvé est le plus récent
    # si l'API respecte order=desc.

    latest_assistant = None

    for message in messages:

        assistant = extract_assistant_message(message)

        if assistant is not None:
            latest_assistant = assistant
            break

    if latest_assistant is None:
        raise RuntimeError(
            "Aucun message assistant trouvé dans Manus"
        )

    latest_text = extract_message_text(
        latest_assistant
    )

    latest_attachments = extract_attachments(
        latest_assistant
    )

    # Recherche du dernier statut disponible.

    manus_status = None

    for message in messages:

        if not isinstance(message, dict):
            continue

        update = message.get("status_update") or {}

        if update.get("agent_status"):
            manus_status = str(
                update["agent_status"]
            ).lower()
            break

    if not manus_status:
        manus_status = "unknown"

    return (
        manus_status,
        latest_text,
        latest_attachments,
        len(messages)
    )


# ============================================================
# IDENTIFIANTS ET NOMS SÛRS
# ============================================================

def safe_job_id(value):

    result = re.sub(
        r"[^a-zA-Z0-9_-]",
        "-",
        str(value)
    ).strip("-")

    if not result:
        raise ValueError(
            "Identifiant de mission invalide"
        )

    return result[:70]


def safe_filename(value, index):

    name = str(
        value or f"livrable_{index}.bin"
    )

    name = name.replace("\\", "/").split("/")[-1]

    name = re.sub(
        r"[^a-zA-Z0-9._ -]",
        "_",
        name
    )

    if name in ("", ".", ".."):
        name = f"livrable_{index}.bin"

    return name[:150]


# ============================================================
# TÉLÉCHARGEMENT SÉCURISÉ
# ============================================================

def get_attachment_url(attachment):

    url = (
        attachment.get("url")
        or attachment.get("download_url")
    )

    if not isinstance(url, str):
        raise ValueError(
            "URL de téléchargement absente"
        )

    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError(
            "URL non HTTPS refusée"
        )

    if not parsed.hostname:
        raise ValueError(
            "Nom d'hôte absent"
        )

    if parsed.username or parsed.password:
        raise ValueError(
            "URL contenant des identifiants refusée"
        )

    return url


def download_file(attachment, destination):

    url = get_attachment_url(attachment)

    size = 0
    digest = hashlib.sha256()

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True
    ) as response:

        response.raise_for_status()

        content_length = response.headers.get(
            "Content-Length"
        )

        if content_length:

            if int(content_length) > MAX_FILE_SIZE:
                raise ValueError(
                    "Fichier supérieur à 10 MB"
                )

        with open(destination, "wb") as output:

            for chunk in response.iter_content(65536):

                if not chunk:
                    continue

                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise ValueError(
                        "Fichier supérieur à 10 MB"
                    )

                digest.update(chunk)

                output.write(chunk)

    if size == 0:

        destination.unlink(missing_ok=True)

        raise ValueError(
            "Fichier vide"
        )

    return {
        "path": destination.name,
        "size": size,
        "sha256": digest.hexdigest()
    }


# ============================================================
# COLLECTE DES FICHIERS
# ============================================================

def collect_files(attachments, files_dir):

    if not attachments:
        raise ValueError(
            "Manus n'a fourni aucune pièce jointe"
        )

    files_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    downloaded = []
    used_names = set()
    total_size = 0

    for index, attachment in enumerate(
        attachments,
        1
    ):

        filename = safe_filename(
            attachment.get("filename")
            or attachment.get("name"),
            index
        )

        stem = Path(filename).stem
        suffix = Path(filename).suffix

        candidate = filename
        counter = 2

        while candidate.lower() in used_names:

            candidate = (
                f"{stem}_{counter}{suffix}"
            )

            counter += 1

        used_names.add(candidate.lower())

        destination = files_dir / candidate

        result = download_file(
            attachment,
            destination
        )

        total_size += result["size"]

        if total_size > MAX_TOTAL_SIZE:
            raise ValueError(
                "Taille totale des livrables supérieure à 40 MB"
            )

        downloaded.append(result)

        logging.info(
            "FICHIER RÉCUPÉRÉ : %s | %s octets",
            candidate,
            result["size"]
        )

    if not downloaded:
        raise ValueError(
            "Aucun fichier valide récupéré"
        )

    return downloaded


# ============================================================
# VALIDATION STRICTE DE LA MISSION
# ============================================================

def check_readiness(
    mission,
    manus_status,
    latest_text
):

    mission_status = str(
        mission.get("status", "")
    ).upper()

    text = str(
        latest_text or ""
    ).upper()

    # Les anciens résultats de db.json ne peuvent plus
    # déclencher une validation à eux seuls.

    if any(word in text for word in BLOCKED_WORDS):

        return (
            False,
            "Manus signale un blocage ou une action humaine"
        )

    if manus_status != "stopped":

        return (
            False,
            f"Manus n'est pas arrêté : {manus_status}"
        )

    # Le marqueur doit être dans le dernier message assistant.
    # Les anciens last_result et execution_summary sont ignorés.

    if not re.search(
        r"(?m)^\s*READY_TO_DELIVER\s*$",
        text
    ):

        return (
            False,
            "Le dernier message Manus ne contient pas "
            "le marqueur READY_TO_DELIVER"
        )

    # Les statuts incohérents sont refusés.

    if mission_status not in {
        "READY_TO_DELIVER",
        "READY_FOR_DELIVERY_REVIEW",
        "EXECUTING",
        "IN_PROGRESS",
        "RUNNING"
    }:

        return (
            False,
            f"Statut de mission incompatible : {mission_status}"
        )

    # Un message final doit comporter un minimum de contenu.

    if len(text.strip()) < 50:

        return (
            False,
            "Dernier message Manus trop court pour être validé"
        )

    return (
        True,
        "Mission validée"
    )


# ============================================================
# PRÉPARATION DU MANIFEST R7
# ============================================================

def create_manifest(
    mission_id,
    mission,
    job_id,
    content,
    files
):

    title = str(
        mission.get("title")
        or "Mission Manus"
    )

    source_url = str(
        mission.get("url")
        or ""
    )

    description = (
        "Livraison préparée automatiquement par TENA IZY.\n\n"
        f"Mission ID : {mission_id}\n"
        f"Source : {source_url}\n\n"
        "Dernier résultat Manus :\n\n"
        + content[-5000:]
    )

    return {
        "job_id": job_id,
        "source": "Manus",
        "repository": REPOSITORY,
        "base_branch": BASE_BRANCH,
        "title": f"Livraison : {title}"[:240],
        "description": description,
        "source_dir": "files",
        "ready": True,
        "delete_paths": [],
        "files": files,
        "created_at": time.time()
    }


# ============================================================
# TRANSFERT D'UNE MISSION
# ============================================================

def process_mission(mission_id, mission):

    job_id = safe_job_id(mission_id)

    job_dir = INBOX / job_id

    manifest_path = job_dir / "delivery.json"

    if manifest_path.exists():

        logging.info(
            "MISSION DÉJÀ TRANSMISE : %s",
            mission_id
        )

        return False

    task_id = mission.get("manus_task_id")

    if not task_id:

        logging.warning(
            "ID MANUS ABSENT : %s",
            mission_id
        )

        return False

    logging.info(
        "EXAMEN : %s | Tâche Manus : %s",
        mission_id,
        task_id
    )

    # --------------------------------------------------------
    # 1. RÉCUPÉRER LE DERNIER MESSAGE
    # --------------------------------------------------------

    try:

        manus_status, content, attachments, message_count = (
            get_manus_result(task_id)
        )

    except Exception as error:

        logging.error(
            "LECTURE MANUS IMPOSSIBLE : %s",
            error
        )

        logging.warning(
            "TRANSFERT REFUSÉ : aucune validation sans "
            "vérification actuelle de Manus"
        )

        return False

    logging.info(
        "STATUT MANUS : %s",
        manus_status
    )

    logging.info(
        "MESSAGES CONSULTÉS : %d",
        message_count
    )

    logging.info(
        "PIÈCES JOINTES DU DERNIER MESSAGE : %d",
        len(attachments)
    )

    # --------------------------------------------------------
    # 2. VÉRIFIER LA FIN RÉELLE DU TRAVAIL
    # --------------------------------------------------------

    ready, reason = check_readiness(
        mission,
        manus_status,
        content
    )

    if not ready:

        logging.warning(
            "TRANSFERT REFUSÉ | %s | %s",
            mission_id,
            reason
        )

        return False

    # --------------------------------------------------------
    # 3. EXIGER LES FICHIERS DU DERNIER MESSAGE
    # --------------------------------------------------------

    if not attachments:

        logging.warning(
            "TRANSFERT REFUSÉ | %s | "
            "Manus indique la fin, mais aucun fichier "
            "n'est joint au dernier message",
            mission_id
        )

        logging.warning(
            "Manus doit joindre les fichiers livrables "
            "à son message final."
        )

        return False

    # --------------------------------------------------------
    # 4. CRÉER UN DOSSIER TEMPORAIRE
    # --------------------------------------------------------

    temp_dir = INBOX / (
        ".tmp_" + job_id + "_" + str(int(time.time()))
    )

    if temp_dir.exists():

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

    temp_dir.mkdir(
        parents=True,
        exist_ok=False
    )

    try:

        # ----------------------------------------------------
        # 5. TÉLÉCHARGER ET CONTRÔLER LES FICHIERS
        # ----------------------------------------------------

        files = collect_files(
            attachments,
            temp_dir / "files"
        )

        if not files:
            raise RuntimeError(
                "Aucun fichier livré"
            )

        # ----------------------------------------------------
        # 6. CRÉER LE MANIFEST
        # ----------------------------------------------------

        manifest = create_manifest(
            mission_id,
            mission,
            job_id,
            content,
            files
        )

        manifest_path_temp = (
            temp_dir / "delivery.json"
        )

        manifest_path_temp.write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        # Vérifier que le JSON est lisible.

        json.loads(
            manifest_path_temp.read_text(
                encoding="utf-8"
            )
        )

        # ----------------------------------------------------
        # 7. FINALISER ATOMIQUEMENT LE TRANSFERT
        # ----------------------------------------------------

        if job_dir.exists():

            raise RuntimeError(
                "Le dossier de livraison existe déjà"
            )

        temp_dir.rename(job_dir)

        logging.info(
            "TRANSFERT R7 RÉUSSI : %s",
            mission_id
        )

        logging.info(
            "FICHIERS TRANSMIS : %d",
            len(files)
        )

        return True

    except Exception as error:

        logging.exception(
            "ÉCHEC DU TRANSFERT : %s | %s",
            mission_id,
            error
        )

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        return False


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    if not MANUS_API_KEY:

        raise RuntimeError(
            "MANUS_API_KEY absent dans les secrets GitHub"
        )

    INBOX.mkdir(
        parents=True,
        exist_ok=True
    )

    missions = load_missions()

    examined = 0
    transferred = 0
    refused = 0
    errors = 0

    logging.info(
        "======================================"
    )

    logging.info(
        "DÉMARRAGE MANUS -> R7"
    )

    logging.info(
        "MISSIONS ENREGISTRÉES : %d",
        len(missions)
    )

    for mission_id, mission in missions.items():

        if not isinstance(mission, dict):
            continue

        status = str(
            mission.get("status", "")
        ).upper()

        if status not in CANDIDATE_STATUSES:
            continue

        if not mission.get("manus_task_id"):
            continue

        examined += 1

        try:

            result = process_mission(
                mission_id,
                mission
            )

            if result:
                transferred += 1
            else:
                refused += 1

        except Exception:

            errors += 1

            logging.exception(
                "ERREUR INATTENDUE : %s",
                mission_id
            )

    logging.info(
        "======================================"
    )

    logging.info(
        "BILAN MANUS -> R7"
    )

    logging.info(
        "Missions examinées : %d",
        examined
    )

    logging.info(
        "Missions transférées : %d",
        transferred
    )

    logging.info(
        "Missions refusées : %d",
        refused
    )

    logging.info(
        "Erreurs : %d",
        errors
    )

    logging.info(
        "======================================"
    )


if __name__ == "__main__":
    main()