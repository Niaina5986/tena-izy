import json
import os
import urllib.request
import urllib.error

from pathlib import Path
from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DB_PATH = ROOT / "db.json"
REPORT_DIR = ROOT / "review_reports"

MANUS_API = "https://api.manus.ai/v2/task.create"

MAX_R8_ATTEMPTS = 1
TIMEOUT = 60


# ============================================================
# UTILITAIRES
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print("[R8 REPAIR AGENT]", message)


def load_json(path):

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):

    db["updated_at"] = now()

    temp = DB_PATH.with_suffix(".json.tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(
            db,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temp, DB_PATH)


# ============================================================
# CONSTRUCTION DE LA MISSION DE RÉPARATION
# ============================================================

def build_prompt(mission, report):

    execution = mission.get("execution", {})

    review = report.get("review", {})

    missing = review.get("missing_evidence", [])

    return f"""
Tu es R8, agent de réparation contrôlée de BOT WORKER.

Une mission a été bloquée par R6-R7.
Ton travail consiste à résoudre les problèmes identifiés
et à fournir des preuves techniques vérifiables.

==================================================
MISSION
==================================================

Titre :
{mission.get("title", "")}

Description :
{mission.get("description", "")}

Source :
{mission.get("url", "")}

==================================================
RAPPORT R6-R7
==================================================

{json.dumps(review, ensure_ascii=False, indent=2)}

==================================================
ANCIEN RAPPORT MANUS
==================================================

{str(execution.get("result") or "")[:12000]}

==================================================
OBJECTIFS DE R8
==================================================

1. Examiner le dépôt réel concerné.

2. Vérifier si le travail précédent existe réellement.

3. Corriger les problèmes non résolus.

4. Exécuter réellement les tests nécessaires.

5. Enregistrer les résultats exacts des tests.

6. Si possible, créer une branche GitHub indépendante.

7. Créer un commit vérifiable si les modifications sont correctes.

8. Fournir un lien réel vers le commit ou la Pull Request.

9. Expliquer précisément les problèmes qui empêchent
   la livraison.

==================================================
PREUVES MANQUANTES
==================================================

{json.dumps(missing, ensure_ascii=False)}

==================================================
RÈGLES OBLIGATOIRES
==================================================

- Ne jamais inventer un commit.
- Ne jamais inventer une Pull Request.
- Ne jamais inventer des résultats de tests.
- Ne jamais déclarer un test réussi sans l'avoir exécuté.
- Ne jamais modifier directement la branche principale.
- Ne jamais fusionner une Pull Request.
- Ne jamais soumettre une mission à une plateforme.
- Ne jamais dépenser d'argent.
- Ne jamais contourner une vérification humaine.
- Ne jamais utiliser de fausses identités.

Si une action nécessite une intervention humaine,
explique clairement laquelle.

==================================================
RAPPORT FINAL OBLIGATOIRE
==================================================

Retourne les informations suivantes :

- artifact_url
- commit_sha
- test_command
- exact_test_results
- files_changed
- blockers
- human_actions
- final_status

final_status doit être :

READY_FOR_REVIEW
BLOCKED
ou
NEEDS_HUMAN

Ne déclare jamais le travail terminé si les preuves
ne sont pas disponibles.
"""


# ============================================================
# CRÉATION DE LA TÂCHE MANUS
# ============================================================

def create_manus_task(mission, report):

    api_key = os.getenv("MANUS_API_KEY")

    if not api_key:
        raise RuntimeError(
            "MANUS_API_KEY manquante."
        )

    prompt = build_prompt(mission, report)

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

        "title": (
            "BOT WORKER R8 - "
            + mission.get("title", "Mission")[:60]
        ),

        "locale": "fr",

        "agent_profile": "standard",

        "interactive_mode": False,

        "share_visibility": "private"
    }

    request = urllib.request.Request(

        MANUS_API,

        data=json.dumps(payload).encode("utf-8"),

        headers={
            "Content-Type": "application/json",
            "x-manus-api-key": api_key
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

    except urllib.error.HTTPError as error:

        details = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"Manus HTTP {error.code}: {details[:1200]}"
        )

    if not result.get("ok"):
        raise RuntimeError(
            "Réponse Manus invalide : "
            + json.dumps(result)
        )

    if not result.get("task_id"):
        raise RuntimeError(
            "Manus n'a pas retourné de task_id."
        )

    return result


# ============================================================
# ANALYSE DES MISSIONS BLOQUÉES
# ============================================================

def process_mission(mission):

    mission_id = str(mission.get("id", ""))

    report_path = REPORT_DIR / f"{mission_id}.json"

    if not report_path.exists():

        log(
            f"{mission_id}: rapport R6-R7 introuvable."
        )

        return False

    report = load_json(report_path)

    review = report.get("review", {})

    execution = mission.get("execution", {})

    decision = review.get("decision")

    status = str(
        execution.get("status", "")
    ).upper()

    # Seulement les missions bloquées pour absence de livraison.

    if decision != "BLOCKED_NO_ARTIFACT":
        return False

    if status != "READY_FOR_REVIEW":
        return False

    # Limite de sécurité.

    attempts = int(
        execution.get("r8_attempts", 0)
    )

    if attempts >= MAX_R8_ATTEMPTS:

        log(
            f"{mission_id}: limite de relance atteinte."
        )

        return False

    # Empêche les doubles exécutions.

    if execution.get("r8_current_task_id"):

        log(
            f"{mission_id}: une tâche R8 existe déjà."
        )

        return False

    log("Mission bloquée détectée.")

    log(mission.get("title", ""))

    # Créer une nouvelle tâche Manus.

    result = create_manus_task(
        mission,
        report
    )

    task_id = result["task_id"]

    task_url = result.get("task_url")

    # Historique de la mission précédente.

    history = execution.setdefault(
        "r8_history",
        []
    )

    history.append({

        "previous_task_id": execution.get("task_id"),

        "previous_task_url": execution.get("task_url"),

        "created_at": now(),

        "review_decision": decision,

        "missing_evidence": review.get(
            "missing_evidence",
            []
        )
    })

    # Enregistrer la nouvelle tâche.

    execution["r8_attempts"] = attempts + 1

    execution["r8_current_task_id"] = task_id

    execution["task_id"] = task_id

    execution["task_url"] = task_url

    execution["started_at"] = now()

    execution["status"] = "RUNNING"

    execution["result"] = None

    execution["submitted"] = False

    execution.pop("monitor_error", None)

    mission["status"] = "IN_PROGRESS"

    log("Nouvelle tâche Manus créée.")

    log("Task ID : " + task_id)

    log("URL : " + str(task_url))

    return True


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():

    log("Démarrage de R8.")

    if not DB_PATH.exists():

        raise FileNotFoundError(
            "db.json introuvable."
        )

    if not REPORT_DIR.exists():

        log("Dossier review_reports introuvable.")

        return

    db = load_json(DB_PATH)

    started = 0

    for mission in db.get("missions", []):

        try:

            if process_mission(mission):
                started += 1

        except Exception as error:

            log(
                f"Erreur mission {mission.get('id')}: {error}"
            )

    if started > 0:

        save_db(db)

        log(
            f"{started} mission(s) relancée(s)."
        )

    else:

        log(
            "Aucune mission éligible à une reprise."
        )

    log("R8 terminé.")


if __name__ == "__main__":
    main()