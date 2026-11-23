#!/usr/bin/env python3
# ============================================================
# TENA IZY — ROBOT CENTRAL AUTONOME
# Recherche → Analyse → Sélection → Exécution
# → Vérification → Livraison → Suivi → Notifications
# ============================================================

import os
import json
import time
import hashlib
import logging
import requests

from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

# ============================================================
# CONFIGURATION
# ============================================================

DB_FILE = Path("db.json")

JOBBI_URL = os.getenv(
    "JOBBI_URL",
    "https://jobbi.org/gigs.json"
)

MANUS_URL = "https://api.manus.ai/v2"

MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))
MAX_TASKS = int(os.getenv("MAX_NEW_TASKS", "1"))

POLL_SECONDS = 45
POLL_LIMIT = 12

TIMEOUT = 40

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TENA IZY] %(levelname)s: %(message)s"
)

# ============================================================
# TEMPS
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# BASE DE DONNÉES
# ============================================================

def load_db():

    if not DB_FILE.exists():
        raise RuntimeError("db.json introuvable.")

    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data.get("missions"), dict):
        raise RuntimeError(
            "Structure db.json incorrecte. Arrêt sécurisé."
        )

    data.setdefault("history", [])
    data.setdefault("telegram", {})
    data.setdefault("system", {})

    return data


def save_db(data):

    data["updated_at"] = now()

    temp = DB_FILE.with_suffix(".json.tmp")

    temp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    temp.replace(DB_FILE)


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logging.warning("Telegram non configuré.")
        return

    try:

        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message[:3900],
                "disable_web_page_preview": True
            },
            timeout=TIMEOUT
        ).raise_for_status()

    except Exception as e:

        logging.error("Erreur Telegram : %s", e)


# ============================================================
# REQUÊTES HTTP
# ============================================================

def get_json(url, headers=None, params=None):

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=TIMEOUT
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# NORMALISATION DES MISSIONS
# ============================================================

def normalize_mission(item, source="Jobbi"):

    if not isinstance(item, dict):
        return None

    title = str(
        item.get("title")
        or item.get("name")
        or item.get("headline")
        or ""
    ).strip()

    description = str(
        item.get("description")
        or item.get("details")
        or item.get("summary")
        or ""
    ).strip()

    url = (
        item.get("url")
        or item.get("link")
        or item.get("gig_url")
    )

    if not title or not url:
        return None

    if not str(url).startswith("http"):
        return None

    mission_id = str(
        item.get("id")
        or hashlib.sha1(
            str(url).encode()
        ).hexdigest()[:12]
    )

    return {
        "id": mission_id,
        "title": title,
        "description": description[:16000],
        "url": url,
        "repository": item.get("repository") or "",
        "created_at": item.get("created_at") or now(),
        "updated_at": item.get("updated_at") or now(),
        "labels": item.get("labels") or [],
        "source": source,
        "status": "NEW",
        "created_in_system": now()
    }


# ============================================================
# MODULE 1 — RECHERCHE
# ============================================================

def search_missions():

    results = []

    feeds = [JOBBI_URL]

    extra_feeds = os.getenv(
        "TASK_FEED_URLS",
        ""
    )

    feeds.extend(
        x.strip()
        for x in extra_feeds.split(",")
        if x.strip()
    )

    for feed in feeds:

        try:

            raw = get_json(feed)

            if isinstance(raw, list):
                items = raw
            else:
                items = (
                    raw.get("gigs")
                    or raw.get("data")
                    or raw.get("items")
                    or []
                )

            source = (
                "Jobbi"
                if feed == JOBBI_URL
                else urlparse(feed).netloc
            )

            for item in items:

                mission = normalize_mission(
                    item,
                    source
                )

                if mission:
                    results.append(mission)

            logging.info(
                "%s : %d missions trouvées",
                source,
                len(items)
            )

        except Exception as e:

            logging.warning(
                "Source indisponible %s : %s",
                feed,
                e
            )

    return results


# ============================================================
# MODULE 2 — ANALYSE
# ============================================================

def analyze_mission(mission):

    text = (
        mission.get("title", "")
        + " "
        + mission.get("description", "")
        + " "
        + " ".join(mission.get("labels", []))
    ).lower()

    positive = [
        "bounty",
        "paid",
        "payment",
        "reward",
        "usd",
        "bug",
        "fix",
        "issue",
        "code",
        "documentation",
        "implement",
        "test",
        "research",
        "development"
    ]

    negative = [
        "unpaid",
        "volunteer",
        "closed",
        "winner takes",
        "private key",
        "wallet seed",
        "malware",
        "credential theft"
    ]

    score = 3

    score += min(
        4,
        sum(1 for word in positive if word in text)
    )

    score -= min(
        5,
        2 * sum(1 for word in negative if word in text)
    )

    if len(mission.get("description", "")) > 300:
        score += 1

    score = max(0, min(10, score))

    risk = (
        "HIGH"
        if any(word in text for word in [
            "private key",
            "wallet seed",
            "malware",
            "credential theft",
            "personal data"
        ])
        else "LOW"
    )

    automation = (
        "HIGH"
        if any(word in text for word in [
            "code",
            "bug",
            "documentation",
            "write",
            "test",
            "research"
        ])
        else "MEDIUM"
    )

    suitable = (
        score >= MIN_SCORE
        and risk == "LOW"
    )

    return {
        "id": mission["id"],
        "score": score,
        "reward_estimate": 0,
        "difficulty": "UNKNOWN",
        "automation": automation,
        "risk": risk,
        "suitable": suitable,
        "explanation": (
            "Pré-analyse automatique. "
            "Récompense et conditions à confirmer."
        )
    }


# ============================================================
# MODULE 3 — EXÉCUTION MANUS
# ============================================================

def start_execution(mission):

    api_key = os.getenv("MANUS_API_KEY")

    if not api_key:
        raise RuntimeError(
            "MANUS_API_KEY manquant."
        )

    prompt = f"""
Tu es l'agent d'exécution de TENA IZY.

MISSION :
{mission['title']}

LIEN :
{mission['url']}

DESCRIPTION :
{mission.get('description', '')[:12000]}

OBJECTIFS :

1. Lire et comprendre toutes les consignes.
2. Vérifier les conditions de rémunération.
3. Vérifier le délai et l'éligibilité.
4. Déterminer si la mission est réellement réalisable.
5. Réaliser le travail demandé.
6. Produire les fichiers nécessaires.
7. Vérifier les résultats.
8. Préparer la livraison.

RÈGLES :

- Ne jamais inventer une réalisation.
- Ne jamais inventer un test réussi.
- Ne jamais inventer un paiement.
- Ne jamais utiliser de compte non autorisé.
- Ne jamais contourner CAPTCHA ou authentification.
- Ne jamais exposer de clé secrète.
- Respecter les règles de la plateforme.

À LA FIN, INDIQUER UN ÉTAT :

READY_TO_DELIVER :
Travail terminé et prêt à être soumis.

NEEDS_HUMAN :
Une connexion, une clé API, une autorisation
ou une intervention humaine est nécessaire.

BLOCKED :
Mission impossible ou non réalisable.

Si une intervention humaine est nécessaire,
indiquer précisément :

- Ce qui manque.
- Le lien officiel de connexion.
- La documentation API officielle.
- Les étapes à effectuer.

Si le travail est terminé, fournir les livrables
et les instructions de livraison.
Ne prétends pas avoir soumis le travail si ce
n'est pas réellement le cas.
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
        "title": "TENA IZY - " + mission["title"][:90],
        "agent_profile": "standard",
        "interactive_mode": False,
        "share_visibility": "private"
    }

    response = requests.post(
        MANUS_URL + "/task.create",
        headers={
            "x-manus-api-key": api_key,
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("task_id"):
        raise RuntimeError(
            "Manus n'a pas retourné task_id."
        )

    return result


# ============================================================
# MODULE 4 — SUIVI MANUS
# ============================================================

def get_manus_messages(task_id):

    api_key = os.getenv("MANUS_API_KEY")

    return get_json(
        MANUS_URL + "/task.listMessages",
        headers={
            "x-manus-api-key": api_key
        },
        params={
            "task_id": task_id,
            "order": "desc",
            "limit": 30
        }
    )


def parse_manus_messages(data):

    status = "running"

    texts = []
    attachments = []

    for message in data.get("messages", []):

        if message.get("type") == "status_update":

            status = (
                message.get("status_update", {})
                .get("agent_status", status)
            )

        assistant = (
            message.get("assistant_message")
            or {}
        )

        if assistant.get("content"):
            texts.append(assistant["content"])

        attachments.extend(
            assistant.get("attachments") or []
        )

    return (
        status,
        "\n\n".join(reversed(texts)),
        attachments
    )


def monitor_execution(mission):

    for _ in range(POLL_LIMIT):

        try:

            data = get_manus_messages(
                mission["manus_task_id"]
            )

            status, content, attachments = (
                parse_manus_messages(data)
            )

            if status in [
                "stopped",
                "waiting",
                "error"
            ]:
                return status, content, attachments

        except Exception as e:

            logging.warning(
                "Erreur suivi Manus : %s",
                e
            )

        time.sleep(POLL_SECONDS)

    return "running", "", []


# ============================================================
# MODULE 5 — LIVRAISON ET INTERVENTION HUMAINE
# ============================================================

def request_human_action(mission, content, attachments):

    message = [
        "🟠 TENA IZY — INTERVENTION NÉCESSAIRE",
        "",
        "Mission : " + mission.get("title", ""),
        "Source : " + mission.get("url", ""),
        "Manus : " + mission.get("manus_task_url", ""),
        "Statut : " + mission.get("status", ""),
        "",
        "Le travail est conservé.",
        "Une connexion, une API ou une autorisation",
        "est nécessaire pour continuer.",
        "",
        "Consulte le lien officiel de la mission",
        "et sa documentation API."
    ]

    for attachment in attachments or []:

        if attachment.get("url"):

            message.append(
                str(attachment.get("filename", "Livrable"))
                + " : "
                + attachment["url"]
            )

    if content:

        message.extend([
            "",
            "Résumé du travail :",
            content[-1500:]
        ])

    message.append(
        "\nNe transmets jamais tes mots de passe ou clés API dans Telegram."
    )

    telegram("\n".join(message))


def process_completed_task(mission, state, content, attachments):

    mission["execution_summary"] = content[-12000:]
    mission["deliverables"] = attachments
    mission["last_result_checked"] = now()

    upper = content.upper()

    if state == "error":

        mission["status"] = "EXECUTION_ERROR"

        telegram(
            "🔴 TENA IZY : erreur d'exécution\n"
            + mission["title"]
            + "\n"
            + content[-1000:]
        )

        return

    if state == "waiting" or "NEEDS_HUMAN" in upper:

        mission["status"] = "WAITING_HUMAN"

        request_human_action(
            mission,
            content,
            attachments
        )

        return

    if "BLOCKED" in upper:

        mission["status"] = "BLOCKED"

        telegram(
            "⛔ TENA IZY : mission bloquée\n"
            + mission["title"]
            + "\n"
            + content[-1200:]
        )

        return

    if "READY_TO_DELIVER" in upper:

        mission["status"] = "READY_TO_DELIVER"

        request_human_action(
            mission,
            content,
            attachments
        )

        return

    # En cas de résultat ambigu, ne pas prétendre
    # que le travail est prêt ou livré.
    mission["status"] = "REVIEW_REQUIRED"

    request_human_action(
        mission,
        content,
        attachments
    )


# ============================================================
# MODULE 6 — ORCHESTRATEUR CENTRAL
# ============================================================

def run_robot():

    data = load_db()

    missions = data["missions"]

    logging.info(
        "Base chargée : %d missions",
        len(missions)
    )

    # --------------------------------------------------------
    # RECHERCHE
    # --------------------------------------------------------

    found = search_missions()

    new_count = 0

    for mission in found:

        mission_id = str(mission["id"])

        if mission_id in missions:
            continue

        mission["analysis"] = analyze_mission(
            mission
        )

        mission["score"] = (
            mission["analysis"]["score"]
        )

        mission["analyzed_at"] = now()

        mission["analysis_model"] = "TENA-IZY"

        missions[mission_id] = mission

        new_count += 1

    logging.info(
        "%d nouvelles missions",
        new_count
    )

    # --------------------------------------------------------
    # REPRISE D'UNE MISSION EN COURS
    # --------------------------------------------------------

    active = None

    for mission in missions.values():

        if (
            mission.get("status") in [
                "EXECUTING",
                "RUNNING"
            ]
            and mission.get("manus_task_id")
        ):

            active = mission
            break

    if active:

        logging.info(
            "Reprise mission : %s",
            active["title"]
        )

        state, content, attachments = monitor_execution(
            active
        )

        if state == "running":

            active["status"] = "EXECUTING"

        else:

            process_completed_task(
                active,
                state,
                content,
                attachments
            )

    # --------------------------------------------------------
    # SÉLECTION D'UNE NOUVELLE MISSION
    # --------------------------------------------------------

    else:

        candidates = []

        for mission in missions.values():

            analysis = mission.get("analysis", {})

            if mission.get("status") != "NEW":
                continue

            if not analysis.get("suitable"):
                continue

            if analysis.get("score", 0) < MIN_SCORE:
                continue

            candidates.append(mission)

        candidates.sort(
            key=lambda m: m.get("score", 0),
            reverse=True
        )

        if candidates:

            mission = candidates[0]

            if not os.getenv("MANUS_API_KEY"):

                telegram(
                    "⚠️ TENA IZY : missions disponibles, "
                    "mais MANUS_API_KEY est manquant."
                )

            else:

                try:

                    result = start_execution(
                        mission
                    )

                    mission["manus_task_id"] = (
                        result["task_id"]
                    )

                    mission["manus_task_url"] = (
                        result.get("task_url", "")
                    )

                    mission["status"] = "EXECUTING"

                    mission["started_at"] = now()

                    data["system"]["current_mission"] = (
                        mission["id"]
                    )

                    telegram(
                        "🤖 TENA IZY démarre une mission\n\n"
                        + mission["title"]
                        + "\n"
                        + mission["url"]
                        + "\n"
                        + mission.get("manus_task_url", "")
                    )

                except Exception as e:

                    mission["status"] = "START_ERROR"

                    mission["last_error"] = str(e)[:1000]

                    logging.exception(
                        "Erreur de démarrage Manus"
                    )

    # --------------------------------------------------------
    # STATISTIQUES
    # --------------------------------------------------------

    data["system"]["last_scan"] = now()

    data["system"]["total_found"] = len(missions)

    data["system"]["total_completed"] = sum(
        1
        for m in missions.values()
        if m.get("status") == "DELIVERED"
    )

    data["system"]["status"] = (
        "RUNNING"
        if active
        else "IDLE"
    )

    data["history"].append({
        "action": "TENA_IZY_RUN",
        "timestamp": now(),
        "new_missions": new_count
    })

    data["history"] = data["history"][-500:]

    save_db(data)

    logging.info(
        "TENA IZY terminé."
    )


# ============================================================
# DÉMARRAGE
# ============================================================

if __name__ == "__main__":

    try:

        run_robot()

    except Exception as error:

        logging.exception(
            "Arrêt sécurisé du robot"
        )

        telegram(
            "🔴 TENA IZY arrêté :\n"
            + str(error)[:1200]
        )

        raise