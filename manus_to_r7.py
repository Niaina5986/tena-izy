import os
import re
import json
import time
import logging
import shutil
import requests

from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "db.json"
INBOX = ROOT / "delivery_inbox"

MANUS_URL = "https://api.manus.ai/v2"
MANUS_API_KEY = os.getenv("MANUS_API_KEY")

REPOSITORY = os.getenv(
    "DELIVERY_REPOSITORY",
    "Niaina5986/tena-izy"
)

BASE_BRANCH = os.getenv("DELIVERY_BASE_BRANCH", "main")

TIMEOUT = 45
MAX_FILE_SIZE = 10 * 1024 * 1024

CANDIDATE_STATUSES = {
    "EXECUTING",
    "RUNNING",
    "IN_PROGRESS",
    "READY_TO_DELIVER",
    "READY_FOR_DELIVERY_REVIEW"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MANUS-R7] %(levelname)s: %(message)s"
)


def load_missions():
    if not DB_FILE.exists():
        raise RuntimeError("db.json introuvable")

    data = json.loads(DB_FILE.read_text(encoding="utf-8"))

    missions = data.get("missions")

    if not isinstance(missions, dict):
        raise RuntimeError("Structure missions invalide")

    return missions


def get_manus_result(task_id):

    response = requests.get(
        MANUS_URL + "/task.listMessages",
        headers={
            "x-manus-api-key": MANUS_API_KEY
        },
        params={
            "task_id": task_id,
            "order": "desc",
            "limit": 30
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    status = "running"
    texts = []
    attachments = []

    for message in data.get("messages", []):

        if message.get("type") == "status_update":
            status = message.get(
                "status_update", {}
            ).get("agent_status", status)

        assistant = message.get("assistant_message") or {}

        if assistant.get("content"):
            texts.append(str(assistant["content"]))

        attachments.extend(
            assistant.get("attachments") or []
        )

    return status, "\n\n".join(reversed(texts)), attachments


def safe_job_id(value):

    result = re.sub(
        r"[^a-zA-Z0-9_-]",
        "-",
        str(value)
    ).strip("-")

    if not result:
        raise ValueError("ID de mission invalide")

    return result[:70]


def safe_filename(value, index):

    name = str(value or f"livrable_{index}.bin")
    name = name.replace("\\", "/").split("/")[-1]

    name = re.sub(
        r"[^a-zA-Z0-9._ -]",
        "_",
        name
    )

    if name in ("", ".", ".."):
        name = f"livrable_{index}.bin"

    return name[:150]


def download_file(attachment, destination):

    url = (
        attachment.get("url")
        or attachment.get("download_url")
    )

    if not isinstance(url, str):
        raise ValueError("URL de fichier absente")

    parsed = urlparse(url)

    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("URL HTTPS invalide")

    response = requests.get(
        url,
        timeout=TIMEOUT,
        stream=True
    )

    response.raise_for_status()

    size = 0

    with open(destination, "wb") as output:

        for chunk in response.iter_content(65536):

            if not chunk:
                continue

            size += len(chunk)

            if size > MAX_FILE_SIZE:
                raise ValueError("Fichier supérieur à 10 MB")

            output.write(chunk)

    if size == 0:
        destination.unlink(missing_ok=True)
        raise ValueError("Fichier vide")

    return size


def collect_files(attachments, files_dir):

    if not attachments:
        raise ValueError("Aucun livrable fourni par Manus")

    files_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    used_names = set()

    for index, attachment in enumerate(attachments, 1):

        if not isinstance(attachment, dict):
            continue

        filename = safe_filename(
            attachment.get("filename") or attachment.get("name"),
            index
        )

        stem = Path(filename).stem
        suffix = Path(filename).suffix

        candidate = filename
        counter = 2

        while candidate.lower() in used_names:
            candidate = f"{stem}_{counter}{suffix}"
            counter += 1

        used_names.add(candidate.lower())

        destination = files_dir / candidate

        size = download_file(
            attachment,
            destination
        )

        downloaded.append({
            "path": candidate,
            "size": size
        })

        logging.info("Fichier récupéré : %s", candidate)

    if not downloaded:
        raise ValueError("Aucun fichier téléchargeable")

    return downloaded


def is_ready(mission, manus_status, content):

    combined = (
        str(content or "")
        + "\n"
        + str(mission.get("execution_summary") or "")
        + "\n"
        + str(mission.get("last_result") or "")
    ).upper()

    if "NEEDS_HUMAN" in combined:
        return False

    if "BLOCKED" in combined:
        return False

    if "READY_TO_DELIVER" not in combined:
        return False

    return (
        manus_status == "stopped"
        or mission.get("status") == "READY_TO_DELIVER"
    )


def process_mission(mission_id, mission):

    job_id = safe_job_id(mission_id)
    job_dir = INBOX / job_id

    if (job_dir / "delivery.json").exists():
        logging.info("Déjà transférée : %s", job_id)
        return False

    task_id = mission.get("manus_task_id")

    if not task_id:
        return False

    try:
        manus_status, content, attachments = get_manus_result(task_id)

    except Exception as error:

        if mission.get("status") != "READY_TO_DELIVER":
            logging.warning(
                "Impossible de consulter Manus : %s",
                error
            )
            return False

        logging.warning(
            "API Manus indisponible, utilisation de db.json"
        )

        manus_status = mission.get("manus_status", "")
        content = mission.get("execution_summary", "")
        attachments = mission.get("deliverables") or []

    if not is_ready(mission, manus_status, content):

        logging.info("Mission non prête : %s", mission_id)
        return False

    if not attachments:
        attachments = mission.get("deliverables") or []

    job_dir.mkdir(parents=True, exist_ok=False)

    try:

        files = collect_files(
            attachments,
            job_dir / "files"
        )

        title = mission.get("title", "Mission Manus")

        description = (
            "Livraison préparée automatiquement par TENA IZY.\n\n"
            f"Mission ID : {mission_id}\n"
            f"Source : {mission.get('url', '')}\n\n"
            "Résumé Manus :\n"
            + str(content or mission.get("execution_summary", ""))[-5000:]
        )

        manifest = {
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

        (job_dir / "delivery.json").write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        logging.info("TRANSFERT R7 RÉUSSI : %s", job_id)

        return True

    except Exception:

        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def main():

    if not MANUS_API_KEY:
        raise RuntimeError("MANUS_API_KEY absent")

    INBOX.mkdir(parents=True, exist_ok=True)

    missions = load_missions()

    examined = 0
    transferred = 0

    for mission_id, mission in missions.items():

        if not isinstance(mission, dict):
            continue

        status = str(mission.get("status", "")).upper()

        if status not in CANDIDATE_STATUSES:
            continue

        if not mission.get("manus_task_id"):
            continue

        examined += 1

        try:

            if process_mission(mission_id, mission):
                transferred += 1

        except Exception:

            logging.exception(
                "Erreur mission %s",
                mission_id
            )

    logging.info(
        "FIN : %d missions examinées, %d transférées",
        examined,
        transferred
    )


if __name__ == "__main__":
    main()