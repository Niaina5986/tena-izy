#!/usr/bin/env python3
# ============================================================
# TENA IZY — MANUS TO R7 BRIDGE
# Manus → Récupération → Vérification → Delivery Inbox
# ============================================================

import os
import re
import json
import time
import logging
import requests

from pathlib import Path
from urllib.parse import urlparse

# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DB_FILE = ROOT / "db.json"
INBOX = ROOT / "delivery_inbox"
DELIVERY_DB = ROOT / "livraison.json"

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MANUS-R7] %(levelname)s: %(message)s"
)

# ============================================================
# HTTP
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "x-manus-api-key": MANUS_API_KEY or "",
    "Accept": "application/json"
})


def manus_get(task_id):

    response = SESSION.get(
        MANUS_URL + "/task.listMessages",
        params={
            "task_id": task_id,
            "order": "desc",
            "limit": 50
        },
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# DATABASE
# ============================================================

def load_db():

    if not DB_FILE.exists():
        raise RuntimeError("db.json introuvable")

    data = json.loads(
        DB_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data.get("missions"), dict):
        raise RuntimeError("Structure missions incorrecte")

    return data


def load_delivery_db():

    if not DELIVERY_DB.exists():
        return {"version": 1, "deliveries": {}}

    data = json.loads(
        DELIVERY_DB.read_text(encoding="utf-8")
    )

    return data


# ============================================================
# MANUS RESPONSE
# ============================================================

def parse_messages(data):

    status = "running"

    texts = []
    attachments = []

    for message in data.get("messages", []):

        if message.get("type") == "status_update":

            update = message.get(
                "status_update", {}
            )

            status = update.get(
                "agent_status",
                status
            )

        assistant = (
            message.get("assistant_message") or {}
        )

        content = assistant.get("content")

        if content:
            texts.append(content)

        files = assistant.get("attachments") or []

        attachments.extend(files)

    return (
        status,
        "\n\n".join(reversed(texts)),
        attachments
    )


# ============================================================
# VALIDATION
# ============================================================

def is_ready(status, content):

    if status != "stopped":
        return False

    text = content.upper()

    if "NEEDS_HUMAN" in text:
        return False

    if "BLOCKED" in text:
        return False

    return "READY_TO_DELIVER" in text


def safe_job_id(value):

    value = re.sub(
        r"[^a-zA-Z0-9_-]",
        "-",
        str(value)
    )

    return value[:70]


# ============================================================
# DOWNLOAD FILES
# ============================================================

def download_attachment(attachment, destination):

    url = (
        attachment.get("url")
        or attachment.get("download_url")
    )

    if not url:
        raise ValueError("URL de fichier absent")

    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError("URL HTTPS obligatoire")

    if not parsed.hostname:
        raise ValueError("Hôte de téléchargement absent")

    response = requests.get(
        url,
        timeout=TIMEOUT,
        stream=True
    )

    response.raise_for_status()

    size = 0

    with open(destination, "wb") as file:

        for chunk in response.iter_content(65536):

            if not chunk:
                continue

            size += len(chunk)

            if size > MAX_FILE_SIZE:
                raise ValueError("Fichier supérieur à 10 MB")

            file.write(chunk)

    if size == 0:
        raise ValueError("Fichier vide")

    return size


def collect_files(attachments, job_dir):

    files_dir = job_dir / "files"

    files_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    downloaded = []

    for index, attachment in enumerate(attachments):

        if not isinstance(attachment, dict):
            continue

        filename = (
            attachment.get("filename")
            or attachment.get("name")
            or f"livrable_{index + 1}.bin"
        )

        filename = Path(filename).name

        if filename in ("", ".", ".."):
            raise ValueError("Nom de fichier invalide")

        destination = files_dir / filename

        size = download_attachment(
            attachment,
            destination
        )

        downloaded.append({
            "filename": filename,
            "size": size
        })

        logging.info(
            "Fichier récupéré : %s (%d octets)",
            filename,
            size
        )

    if not downloaded:
        raise ValueError(
            "Manus n'a fourni aucun fichier téléchargeable"
        )

    return downloaded


# ============================================================
# DELIVERY MANIFEST
# ============================================================

def create_manifest(
    mission,
    job_id,
    job_dir,
    content,
    downloaded
):

    manifest = {

        "job_id": job_id,

        "source": "Manus",

        "repository": REPOSITORY,

        "base_branch": BASE_BRANCH,

        "title": (
            "Livraison : "
            + mission.get("title", "Mission Manus")
        ),

        "description": (
            "Livraison automatique préparée par TENA IZY.\n\n"
            "Mission : "
            + mission.get("url", "")
            + "\n\nRésumé Manus :\n"
            + content[-5000:]
        ),

        "source_dir": "files",

        "ready": True,

        "delete_paths": [],

        "files": downloaded,

        "created_at": time.time()

    }

    path = job_dir / "delivery.json"

    path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# PROCESS MISSION
# ============================================================

def process_mission(mission_id, mission, delivery_db):

    task_id = mission.get("manus_task_id")

    if not task_id:
        return

    job_id = safe_job_id(mission_id)

    job_dir = INBOX / job_id

    if job_id in delivery_db.get("deliveries", {}):

        logging.info(
            "Mission déjà livrée : %s",
            job_id
        )

        return

    if job_dir.exists():

        logging.info(
            "Mission déjà présente dans inbox : %s",
            job_id
        )

        return

    logging.info(
        "Vérification Manus : %s",
        mission.get("title", mission_id)
    )

    data = manus_get(task_id)

    status, content, attachments = parse_messages(data)

    if not is_ready(status, content):

        logging.info(
            "Mission non prête : %s (%s)",
            mission_id,
            status
        )

        return

    logging.info(
        "MISSION PRÊTE : %s",
        mission_id
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=False
    )

    try:

        downloaded = collect_files(
            attachments,
            job_dir
        )

        create_manifest(
            mission,
            job_id,
            job_dir,
            content,
            downloaded
        )

        logging.info(
            "Mission transférée vers R7 : %s",
            job_id
        )

    except Exception:

        import shutil

        shutil.rmtree(
            job_dir,
            ignore_errors=True
        )

        raise


# ============================================================
# MAIN
# ============================================================

def main():

    if not MANUS_API_KEY:

        raise RuntimeError(
            "MANUS_API_KEY absent"
        )

    INBOX.mkdir(
        parents=True,
        exist_ok=True
    )

    data = load_db()

    delivery_db = load_delivery_db()

    missions = data["missions"]

    total = 0

    for mission_id, mission in missions.items():

        if mission.get("status") not in (
            "EXECUTING",
            "RUNNING",
            "READY_TO_DELIVER"
        ):
            continue

        try:

            process_mission(
                mission_id,
                mission,
                delivery_db
            )

            total += 1

        except Exception as error:

            logging.exception(
                "Erreur mission %s : %s",
                mission_id,
                error
            )

    logging.info(
        "TRANSFERT TERMINÉ : %d missions examinées",
        total
    )


if __name__ == "__main__":

    main()