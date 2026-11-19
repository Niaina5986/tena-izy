import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

VERSION = "2.1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PRIMARY_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.8-flash"
)

FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash"
]

MAX_RESULTS = 30
MAX_RETRIES = 3

ACTIVE_STATUSES = {
    "APPROVED",
    "IN_PROGRESS",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
    "BLOCKED"
}

SEARCH_QUERIES = [
    '"$" bounty is:issue is:open',
    'reward is:issue is:open',
    'bounty is:issue is:open',
    'paid task is:issue is:open',
    'good first issue is:issue is:open'
]


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[BOT WORKER] {message}", flush=True)


def request_json(url, headers=None, method="GET", data=None):
    headers = dict(headers or {})

    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(req, timeout=45) as response:
        content = response.read().decode("utf-8")
        return json.loads(content) if content else {}


# ==================================================
# DATABASE
# ==================================================

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

    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    db.setdefault("missions", [])
    db.setdefault("history", [])
    db.setdefault("system", {})

    return db


def save_db(db):
    db["updated_at"] = now()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)


# ==================================================
# VERROU UNE MISSION
# ==================================================

def get_active_mission(db):
    for mission in db["missions"]:
        if mission.get("status") in ACTIVE_STATUSES:
            return mission

    return None


# ==================================================
# GITHUB SEARCH
# ==================================================

def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BOT-WORKER"
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def search_github():
    log("Recherche GitHub...")

    found = {}

    for query in SEARCH_QUERIES:

        params = urllib.parse.urlencode({
            "q": query,
            "sort": "created",
            "order": "desc",
            "per_page": 20
        })

        url = "https://api.github.com/search/issues?" + params

        try:
            result = request_json(url, github_headers())

            for item in result.get("items", []):

                if item.get("pull_request"):
                    continue

                mission_id = str(item["id"])

                found[mission_id] = {
                    "id": mission_id,
                    "title": item.get("title", ""),
                    "description": (
                        item.get("body") or ""
                    )[:5000],
                    "url": item.get("html_url", ""),
                    "repository": item.get("repository_url", ""),
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
            log(f"Erreur GitHub : {e}")

    missions = list(found.values())[:MAX_RESULTS]

    log(f"{len(missions)} missions trouvées.")

    return missions


# ==================================================
# ENREGISTRER LES MISSIONS
# ==================================================

def register_missions(db, new_missions):

    existing = {
        str(m.get("id"))
        for m in db["missions"]
    }

    added = 0

    for mission in new_missions:

        if mission["id"] in existing:
            continue

        mission["created_in_system"] = now()
        mission["status"] = "NEW"

        db["missions"].append(mission)
        existing.add(mission["id"])

        added += 1

    db["system"]["total_found"] = len(db["missions"])

    log(f"{added} nouvelles missions enregistrées.")


# ==================================================
# GEMINI AVEC RETRIES ET FALLBACK
# ==================================================

def call_gemini(model, prompt):

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
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

    headers = {
        "x-goog-api-key": GEMINI_API_KEY
    }

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            log(
                f"Gemini {model} - tentative "
                f"{attempt}/{MAX_RETRIES}"
            )

            response = request_json(
                url,
                headers,
                "POST",
                payload
            )

            text = (
                response["candidates"][0]
                ["content"]["parts"][0]["text"]
            )

            return json.loads(text)

        except urllib.error.HTTPError as e:

            details = e.read().decode(
                "utf-8", errors="replace"
            )

            retryable = e.code in (
                429, 500, 502, 503, 504
            )

            log(f"Gemini HTTP {e.code}")

            if not retryable:
                raise RuntimeError(
                    f"Erreur Gemini permanente : {details[:300]}"
                )

            if attempt < MAX_RETRIES:
                delay = 5 * attempt
                log(f"Nouvelle tentative dans {delay}s")
                time.sleep(delay)

        except Exception as e:

            log(f"Erreur Gemini : {e}")

            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)

    raise RuntimeError(
        f"Modèle {model} indisponible après {MAX_RETRIES} tentatives"
    )


def analyze_with_gemini(missions):

    if not GEMINI_API_KEY:
        log("Clé Gemini absente.")
        return False

    if not missions:
        return True

    compact = []

    for mission in missions:
        compact.append({
            "id": mission["id"],
            "title": mission["title"],
            "description": mission["description"][:1200],
            "url": mission["url"]
        })

    prompt = f"""
Tu es l'analyste du système BOT WORKER.

Analyse les opportunités suivantes.

Pour chaque mission, retourne :

- id
- score de 0 à 10
- reward_estimate en dollars
- difficulty : EASY, MEDIUM ou HARD
- automation : HIGH, MEDIUM ou LOW
- risk : LOW, MEDIUM ou HIGH
- suitable : true ou false
- explanation

RÈGLES :

Ne fabrique jamais de rémunération.
Si le paiement n'est pas confirmé, indique 0.
Ne considère pas une mission comme payée sans preuve.
Vérifie les dépendances et les prérequis.
Écarte les missions frauduleuses.
Ne propose pas de faux comptes ou de fausses identités.
Ne contourne pas les vérifications humaines.
Favorise les tâches techniquement réalisables.
Une mission bloquée par des prérequis ne doit pas être
considérée comme immédiatement exécutable.

Réponds uniquement en JSON valide :

{{
  "results": [
    {{
      "id": "123",
      "score": 5,
      "reward_estimate": 0,
      "difficulty": "MEDIUM",
      "automation": "MEDIUM",
      "risk": "LOW",
      "suitable": true,
      "explanation": "Explication"
    }}
  ]
}}

MISSIONS :

{json.dumps(compact, ensure_ascii=False)}
"""

    models = []

    for model in [PRIMARY_MODEL] + FALLBACK_MODELS:
        if model not in models:
            models.append(model)

    for model in models:

        try:
            log(f"Analyse avec {model}")

            result = call_gemini(model, prompt)

            scores = {
                str(item["id"]): item
                for item in result.get("results", [])
            }

            for mission in missions:

                analysis = scores.get(mission["id"])

                if not analysis:
                    continue

                mission["analysis"] = analysis
                mission["score"] = analysis.get("score", 0)
                mission["analyzed_at"] = now()
                mission["analysis_model"] = model

            log(f"Analyse réussie avec {model}")

            return True

        except Exception as e:
            log(f"Modèle {model} échoué : {e}")

    log("Tous les modèles Gemini ont échoué.")

    return False


# ==================================================
# SÉLECTION
# ==================================================

def select_best_mission(db):

    candidates = []

    for mission in db["missions"]:

        if mission.get("status") != "NEW":
            continue

        analysis = mission.get("analysis", {})

        if not analysis:
            continue

        if not analysis.get("suitable", False):
            continue

        if analysis.get("risk") == "HIGH":
            continue

        if mission.get("score", 0) < 5:
            continue

        candidates.append(mission)

    if not candidates:
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


# ==================================================
# CYCLE PRINCIPAL
# ==================================================

def run():

    log("BOT WORKER " + VERSION)
    log("Démarrage du cycle.")

    db = load_db()

    active = get_active_mission(db)

    if active:
        log("MISSION ACTIVE : " + active.get("title", ""))
        log("Aucune autre mission ne sera lancée.")
        return

    db["system"]["status"] = "SEARCHING"

    # Recherche de nouvelles missions
    new_missions = search_github()

    register_missions(db, new_missions)

    # Sauvegarder avant l'analyse
    save_db(db)

    # Reprendre toutes les missions encore non analysées
    pending = [
        m for m in db["missions"]
        if (
            m.get("status") == "NEW"
            and not m.get("analysis")
        )
    ]

    pending = pending[:MAX_RESULTS]

    log(f"{len(pending)} missions à analyser.")

    if pending:

        success = analyze_with_gemini(pending)

        if not success:
            db["system"]["status"] = "WAITING_GEMINI"
            db["system"]["last_scan"] = now()
            save_db(db)

            log("Analyse reportée au prochain lancement.")
            return

    # Sauvegarder les analyses
    save_db(db)

    # Sélectionner une mission
    best = select_best_mission(db)

    if best:

        best["status"] = "WAITING_APPROVAL"
        best["selected_at"] = now()

        best["execution"] = {
            "status": "NOT_STARTED",
            "executor": None,
            "task_id": None,
            "result": None,
            "submitted": False
        }

        db["system"]["status"] = "WAITING_APPROVAL"
        db["system"]["current_mission"] = best["id"]

        log("MISSION SÉLECTIONNÉE")
        log(best["title"])
        log("Score : " + str(best.get("score", 0)))
        log("En attente de validation humaine.")

    else:

        db["system"]["status"] = "IDLE"
        db["system"]["current_mission"] = None

        log("Aucune mission adaptée.")

    db["system"]["last_scan"] = now()

    save_db(db)

    log("CYCLE TERMINÉ.")


if __name__ == "__main__":

    try:
        run()

    except Exception as error:
        log(f"ERREUR CRITIQUE : {error}")
        raise