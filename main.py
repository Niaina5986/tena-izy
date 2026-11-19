import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

VERSION = "2.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

MAX_RESULTS = 30

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
    print(f"[BOT WORKER] {message}")


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

    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else {}

    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {details[:500]}")


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


def get_active_mission(db):
    for mission in db["missions"]:
        if mission.get("status") in ACTIVE_STATUSES:
            return mission
    return None


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
                    "description": (item.get("body") or "")[:5000],
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
            log(f"Erreur recherche : {e}")

    missions = list(found.values())[:MAX_RESULTS]

    log(f"{len(missions)} missions trouvées.")

    return missions


def analyze_with_gemini(missions):
    if not GEMINI_API_KEY:
        log("Clé Gemini absente.")
        return missions

    if not missions:
        return missions

    log("Analyse des missions avec Gemini...")

    compact = [
        {
            "id": m["id"],
            "title": m["title"],
            "description": m["description"][:1200],
            "url": m["url"]
        }
        for m in missions
    ]

    prompt = f"""
Analyse ces opportunités de travail rémunéré.

Pour chaque mission, retourne :
- id
- score de 0 à 10
- reward_estimate en dollars, uniquement si identifiable
- difficulty : EASY, MEDIUM ou HARD
- automation : HIGH, MEDIUM ou LOW
- risk : LOW, MEDIUM ou HIGH
- suitable : true ou false
- explanation

Règles :
Ne fabrique jamais de rémunération.
Ne suppose pas qu'une mission est payée sans preuve.
Écarte les missions frauduleuses ou trompeuses.
Écarte les tâches nécessitant une fausse identité.
Ne contourne aucune vérification humaine.
Favorise les missions réalisables techniquement.

Réponds uniquement avec un JSON valide :

{{
 "results": []
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
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }

    try:
        response = request_json(
            url,
            {"x-goog-api-key": GEMINI_API_KEY},
            "POST",
            payload
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

    return missions


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
        return None

    candidates.sort(
        key=lambda m: (
            m.get("score", 0),
            m.get("analysis", {}).get("reward_estimate", 0)
        ),
        reverse=True
    )

    return candidates[0]


def run():
    log("DÉMARRAGE BOT WORKER " + VERSION)

    db = load_db()

    active = get_active_mission(db)

    if active:
        log("MISSION ACTIVE : " + active.get("title", ""))
        log("Aucune nouvelle mission ne sera lancée.")
        return

    db["system"]["status"] = "SEARCHING"

    missions = search_github()

    missions = analyze_with_gemini(missions)

    register_missions(db, missions)

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

        log("MISSION SÉLECTIONNÉE : " + best["title"])
        log("Score : " + str(best.get("score", 0)))
        log("En attente de validation.")

    else:
        db["system"]["status"] = "IDLE"
        db["system"]["current_mission"] = None
        log("Aucune mission adaptée.")

    db["system"]["last_scan"] = now()

    save_db(db)

    log("CYCLE TERMINÉ.")


if __name__ == "__main__":
    run()