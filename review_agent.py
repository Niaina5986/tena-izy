import json
import os
import re
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
DB = ROOT / "db.json"
REPORTS = ROOT / "review_reports"

REPORTS.mkdir(exist_ok=True)


def groq_review(mission, evidence):
    key = os.getenv("GROQ_API_KEY")

    if not key:
        return {
            "decision": "UNCERTAIN",
            "reason": "GROQ_API_KEY absent"
        }

    prompt = f"""
Tu es un auditeur indépendant de travaux informatiques.

Analyse cette mission et ses preuves.
Ne crois pas automatiquement le compte rendu de l'exécuteur.

Pour approuver, il faut :
- un véritable lien GitHub vers un commit ou une Pull Request ;
- des preuves de tests exécutés ;
- aucun risque critique identifié.

Réponds uniquement en JSON :
{{
 "decision": "PASS, FAIL ou UNCERTAIN",
 "risks": [],
 "missing_evidence": [],
 "summary": ""
}}

MISSION :
{json.dumps(mission, ensure_ascii=False)}

PREUVES :
{json.dumps(evidence, ensure_ascii=False)}
"""

    body = json.dumps({
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.1,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"}
    }).encode()

    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode())

        return json.loads(
            result["choices"][0]["message"]["content"]
        )

    except Exception as error:
        return {
            "decision": "UNCERTAIN",
            "reason": str(error)
        }


def main():
    if not DB.exists():
        print("db.json introuvable")
        return

    db = json.loads(DB.read_text(encoding="utf-8"))

    for mission in db.get("missions", []):
        execution = mission.get("execution", {})

        status = str(
            execution.get("status", "")
        ).upper()

        if status != "READY_FOR_REVIEW":
            continue

        mission_id = str(mission.get("id", "unknown"))

        artifact_url = (
            execution.get("pr_url")
            or execution.get("commit_url")
            or execution.get("patch_url")
            or execution.get("artifact_url")
        )

        valid_url = (
            isinstance(artifact_url, str)
            and re.match(
                r"^https://github\.com/[^/]+/[^/]+/(pull|commit|tree|compare)/",
                artifact_url
            )
        )

        if not valid_url:
            review = {
                "decision": "BLOCKED_NO_ARTIFACT",
                "summary": "Aucune livraison GitHub vérifiable.",
                "missing_evidence": [
                    "Lien vers le commit ou Pull Request",
                    "Résultats des tests"
                ],
                "automatic_approval": False
            }
        else:
            evidence = {
                "artifact_url": artifact_url,
                "result": execution.get("result", ""),
                "test_logs": execution.get("test_logs", ""),
                "commit_sha": execution.get("commit_sha", "")
            }

            review = groq_review(mission, evidence)

            review["automatic_approval"] = (
                review.get("decision") == "PASS"
            )

        report = {
            "mission_id": mission_id,
            "title": mission.get("title"),
            "reviewed_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "review": review
        }

        destination = REPORTS / f"{mission_id}.json"

        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        print(
            f"Mission {mission_id}: "
            f"{review.get('decision')}"
        )


if __name__ == "__main__":
    main()