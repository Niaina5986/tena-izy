#!/usr/bin/env python3
"""Verify completed Manus mission reports before delivery.

This verifier does not pretend to run a target project's test suite. It checks
the evidence recorded by Manus, policy exclusions, and whether the report is
complete enough for a human/repository delivery check.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "db.json"

BLOCKED_LABELS = {
    "autofix-declined", "bug-me-not", "no-ai", "ai-not-allowed",
    "human-only", "do-not-automate", "no-bots", "no-automated-pr",
}
HUMAN_ONLY_PATTERNS = [
    r"authored and submitted by a human",
    r"not opened autonomously by an ai",
    r"no ai assistance",
    r"human[- ]only",
    r"must be submitted by a human",
    r"do not use ai",
]
BLOCKED_STATES = {"BLOCKED", "NEEDS_HUMAN", "NEEDS_INPUT"}


def now():
    return datetime.now(timezone.utc).isoformat()


def load_db():
    if not DB.exists():
        raise FileNotFoundError("db.json introuvable.")
    data = json.loads(DB.read_text(encoding="utf-8"))
    if not isinstance(data.get("missions"), list):
        raise ValueError("Format db.json invalide: missions doit être une liste.")
    return data


def save_db(data):
    data["updated_at"] = now()
    tmp = DB.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, DB)


def policy_block(mission):
    labels = {str(x).strip().lower() for x in mission.get("labels", [])}
    found = sorted(labels & BLOCKED_LABELS)
    text = (mission.get("title", "") + "\n" + mission.get("description", "")).lower()
    for pattern in HUMAN_ONLY_PATTERNS:
        if re.search(pattern, text, re.I):
            found.append("human-only requirement: " + pattern)
    return found


def verify(mission):
    execution = mission.setdefault("execution", {})
    report = str(execution.get("result") or "").strip()
    reasons = policy_block(mission)

    if reasons:
        return False, "POLICY_BLOCKED: " + "; ".join(reasons)

    if not report:
        return False, "Rapport Manus vide: aucune preuve de travail."

    upper = report.upper()
    if any(state in upper for state in ("FINAL STATE: BLOCKED", "ÉTAT FINAL: BLOCKED",
                                         "FINAL STATE: NEEDS_HUMAN", "ÉTAT FINAL: NEEDS_HUMAN")):
        return False, "Manus indique que la mission est bloquée ou nécessite une intervention."

    # Require some concrete evidence, not just a generic success sentence.
    evidence_terms = ("file", "fichier", "commit", "pull request", "pr #",
                      "test", "tests", "artifact", "livrable", "http")
    if not any(term in report.lower() for term in evidence_terms):
        return False, "Preuves insuffisantes: aucun fichier, test, commit, PR ou livrable identifié."

    execution["verification"] = {
        "status": "VERIFIED_REPORT",
        "checked_at": now(),
        "note": "Rapport et éléments déclaratifs vérifiés. Les tests du dépôt cible ne sont pas exécutés ici."
    }
    return True, "Rapport suffisamment documenté pour le contrôle de livraison."


def main():
    data = load_db()
    changed = False
    count = 0

    for mission in data["missions"]:
        execution = mission.get("execution") or {}
        if execution.get("status") != "READY_FOR_REVIEW":
            continue

        ok, note = verify(mission)
        execution["verification"] = {
            "status": "PASSED" if ok else "FAILED",
            "checked_at": now(),
            "note": note,
        }
        if ok:
            execution["status"] = "VERIFIED"
            mission["status"] = "VERIFIED"
        else:
            execution["status"] = "VERIFICATION_BLOCKED"
            mission["status"] = "BLOCKED"
        changed = True
        count += 1
        print(f"[VERIFIER] {mission.get('id')} -> {execution['status']}: {note}")

    if changed:
        save_db(data)
    print(f"[VERIFIER] {count} mission(s) examinée(s).")


if __name__ == "__main__":
    main()
