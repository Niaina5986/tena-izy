import os
import json
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# =========================================================
# BOT WORKER - MOTEUR PRINCIPAL
# =========================================================

VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

MAX_RESULTS = 30

ACTIVE_STATUSES = {
    "IN_PROGRESS",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "BLOCKED"
}


# =========================================================
# OUTILS
# =========================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def request_json(url, headers=None, method="GET", data=None):
    headers = headers or {}

    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else {}

    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {e.code}: {details[:500]}"
        )

    except Exception as e:
        raise RuntimeError(str(e))


# =========================================================
# BASE DE DONNÉES JSON
# =========================================================

def default_db():
    return {
        "version": VERSION,
        "updated_at": now(),
        "missions": [],
        "history": [],
        "system": {
            "status": "STARTING",
            "current_mission": None,
            "last_scan": None,
            "total_found": 0,
            "total_completed": 0
        }
    }


def load_db():
    if not os.path.exists(DB_FILE):
        return default_db()

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)

        db.setdefault("missions", [])
        db.setdefault("history", [])
        db.setdefault("system", {})

        return db

    except Exception as e:
        log(f"Erreur lecture DB : {e}")
        raise


def save_db(db):
    db["updated_at"] = now()

    temp_file = DB_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp_file, DB_FILE)


# =========================================================
# VERROU : UNE MISSION À LA FOIS
# =========================================================

def get_active_mission(db):
    for mission in db["missions"]:
        if mission.get("status") in ACTIVE_STATUSES:
            return mission

    return None


def system_locked(db):
    mission = get_active_mission(db)

    if mission:
        log(
            "SYSTÈME EN PAUSE : mission active "
            + mission.get("title", "Sans titre")
        )
        return True

    return False


# =========================================================
# RECHERCHE DE MISSIONS GITHUB
# =========================================================

SEARCH_QUERIES = [
    '"$" bounty is:issue is:open',
    'reward is:issue is:open',
    'bounty is:issue is:open',
    'paid task is:issue is:open',
    'good first issue is:issue is:open'
]


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BOT-WORKER"
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def search_github():
    log("Recherche des opportunités GitHub...")

    found = {}

    for query in SEARCH_QUERIES:
        params = urllib.parse.urlencode({
            "q": query,
            "sort": "created",
            "order": "desc",
            "per_page": 20
        })

        url = (
            "https://api.github.com/search/issues?"
            + params
        )

        try:
            result = request_json(
                url,
                headers=github_headers()
            )

            for item in result.get("items", []):
                if item.get("pull_request"):
                    continue

                mission_id = str(item.get("id"))

                found[mission_id] = {
                    "id": mission_id,
                    "title": item.get("title", ""),
                    "description": (
                        item.get("body") or ""
                    )[:5000],
                    "url": item.get("html_url", ""),
                    "repository": item.get(
                        "repository_url", ""
                    ),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "labels": [
                        label.get("name", "")
                        for label in item.get("labels", [])
                    ],
                    "source": "GitHub",
                    "status": "NEW"
                }

            time.sleep(1)

        except Exception as e:
            log(f"Recherche GitHub : {e}")

    missions = list(found.values())[:MAX_RESULTS]

    log(f"{len(missions)} opportunités trouvées.")

    return missions


# =========================================================
# ANALYSE GEMINI
# =========================================================

def analyze_with_gemini(missions):
    if not GEMINI_API_KEY:
        log("Clé Gemini absente : classement automatique désactivé.")
        return missions

    if not missions:
        return missions

    log("Analyse intelligente avec Gemini...")

    compact = []

    for m in missions:
        compact.append({
            "id": m["id"],
            "title": m["title"],
            "description": m["description"][:1200],
            "url": m["url"],
            "labels": m["labels"]
        })

    prompt = f"""
Tu es l'analyste d'un système de recherche de missions rémunérées.

Analyse les missions suivantes.

Pour chaque mission, estime :
- score : de 0 à 10
- reward_estimate : rémunération estimée en dollars, uniquement si
  elle est explicitement identifiable. Sinon 0.
- difficulty : EASY, MEDIUM ou HARD
- automation : HIGH, MEDIUM ou LOW
- risk : LOW, MEDIUM ou HIGH
- explanation : raison courte
- suitable : true ou false

Règles :
- Ne fabrique jamais une rémunération.
- Ne considère pas une mission comme payée sans preuve.
- Refuse les missions qui demandent de tromper une plateforme,
  créer de faux comptes, publier de faux avis ou contourner
  une vérification humaine.
- Ne suppose pas qu'une mission est encore disponible sans vérifier.
- Favorise les tâches réalisables avec du code, des API,
  de l'analyse ou des tests autorisés.

Réponds uniquement avec un JSON valide sous cette forme :

{{
  "results": [
    {{
      "id": "identifiant",
      "score": 0,
      "reward_estimate": 0,
      "difficulty": "MEDIUM",
      "automation": "MEDIUM",
      "risk": "LOW",
      "explanation": "Explication",
      "suitable": true
    }}
  ]
}}

MISSIONS :
{json.dumps(compact, ensure_ascii=False)}
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }

    try:
        response = request_json(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY
            },
            method="POST",
            data=payload
        )

        text = response["candidates"][0]["content"]["parts"][0]["text"]
        analysis = json.loads(text)

        scores = {
            str(item["id"]): item
            for item in analysis.get("results", [])
        }

        for mission in missions:
            result = scores.get(mission["id"])

            if result:
                mission["analysis"] = result
                mission["score"] = result.get("score", 0)
            else:
                mission["score"] = 0

    except Exception as e:
        log(f"Erreur Gemini : {e}")

        for mission in missions:
            mission["score"] = 0

    return missions


# =========================================================
# ENREGISTREMENT DES NOUVELLES MISSIONS
# =========================================================

def register_missions(db, new_missions):
    existing_ids = {
        str(m.get("id"))
        for m in db["missions"]
    }

    added = 0

    for mission in new_missions:
        mission_id = str(mission["id"])

        if mission_id in existing_ids:
            continue

        mission["created_in_system"] = now()
        mission["status"] = "NEW"

        db["missions"].append(mission)
        existing_ids.add(mission_id)

        added += 1

    db["system"]["total_found"] = len(db["missions"])

    log(f"{added} nouvelles missions enregistrées.")

    return added


# =========================================================
# SÉLECTION DE LA MEILLEURE MISSION
# =========================================================

def select_best_mission(db):
    candidates = []

    for mission in db["missions"]:
        if mission.get("status") != "NEW":
            continue

        analysis = mission.get("analysis", {})

        if not analysis.get("suitable", False):
            continue

        if analysis.get("risk") == "HIGH":
            continue

        if mission.get("score", 0) < 5:
            continue

        candidates.append(mission)

    if not candidates:
        log("Aucune mission suffisamment intéressante.")
        return None

    candidates.sort(
        key=lambda m: (
            m.get("score", 0),
            m.get("analysis", {}).get(
                "reward_estimate", 0
            )
        ),
        reverse=True
    )

    return candidates[0]


# =========================================================
# PRÉPARATION DE LA MISSION
# =========================================================

def prepare_mission(db, mission):
    mission["status"] = "WAITING_APPROVAL"
    mission["selected_at"] = now()

    mission["execution"] = {
        "status": "NOT_STARTED",
        "executor": None,
        "result": None,
        "submitted": False
    }

    db["system"]["status"] = "WAITING_APPROVAL"
    db["system"]["current_mission"] = mission["id"]

    log("MEILLEURE MISSION SÉLECTIONNÉE")
    log(f"Titre : {mission['title']}")
    log(f"Score : {mission.get('score', 0)}/10")
    log(f"Lien : {mission['url']}")
    log("En attente de validation humaine.")

    return mission


# =========================================================
# CYCLE PRINCIPAL
# =========================================================

def run():
    log("========================================")
    log("BOT WORKER - DÉMARRAGE")
    log(f"Version : {VERSION}")
    log("========================================")

    db = load_db()

    # Ne jamais commencer une deuxième mission.
    if system_locked(db):
        db["system"]["status"] = "PAUSED"
        save_db(db)
        return

    db["system"]["status"] = "SEARCHING"

    # 1. Recherche
    missions = search_github()

    # 2. Analyse
    missions = analyze_with_gemini(missions)

    # 3. Enregistrement
    register_missions(db, missions)

    # 4. Sélection
    best = select_best_mission(db)

    if best:
        prepare_mission(db, best)
    else:
        db["system"]["status"] = "IDLE"
        db["system"]["current_mission"] = None
        log("Aucune mission sélectionnée.")

    db["system"]["last_scan"] = now()

    save_db(db)

    log("Cycle terminé.")
    log("========================================")


if __name__ == "__main__":
    try:
        run()

    except Exception as error:
        log(f"ERREUR CRITIQUE : {error}")
        raise