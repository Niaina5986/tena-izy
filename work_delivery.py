#!/usr/bin/env python3
"""Confirm a real GitHub PR/commit exists before marking a mission delivered.

This module deliberately does not invent a PR, submit a bounty, merge code,
or claim payment. Manus must provide a real PR URL in its report or in
mission.execution.delivery.pr_url.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "db.json"
GH_TOKEN = os.getenv("GH_PAT", "") or os.getenv("GITHUB_TOKEN", "")
API = "https://api.github.com"


def now():
    return datetime.now(timezone.utc).isoformat()


def load_db():
    data = json.loads(DB.read_text(encoding="utf-8"))
    if not isinstance(data.get("missions"), list):
        raise ValueError("db.json invalide: missions.")
    return data


def save_db(data):
    data["updated_at"] = now()
    tmp = DB.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, DB)


def find_pr_url(mission):
    delivery = (mission.get("execution") or {}).get("delivery") or {}
    candidates = [
        delivery.get("pr_url"),
        (mission.get("execution") or {}).get("pr_url"),
    ]
    report = str((mission.get("execution") or {}).get("result") or "")
    candidates.extend(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", report))
    for value in candidates:
        if value and re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", str(value).rstrip(".,);")):
            return str(value).rstrip(".,);")
    return None


def github_get(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Tena-Izy-Delivery-Agent",
    }
    if GH_TOKEN:
        headers["Authorization"] = "Bearer " + GH_TOKEN
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_pr(pr_url):
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError("URL de Pull Request invalide.")
    owner, repo, number = match.groups()
    pr = github_get(f"{API}/repos/{owner}/{repo}/pulls/{number}")
    if pr.get("html_url") != pr_url:
        raise ValueError("L'URL retournée par GitHub ne correspond pas.")
    if pr.get("state") != "open":
        raise ValueError(f"Pull Request non ouverte (state={pr.get('state')}).")
    return {
        "pr_url": pr["html_url"],
        "number": pr["number"],
        "draft": bool(pr.get("draft", False)),
        "head_branch": (pr.get("head") or {}).get("ref"),
        "base_branch": (pr.get("base") or {}).get("ref"),
        "verified_at": now(),
        "status": "DELIVERED",
    }


def main():
    data = load_db()
    changed = False
    processed = 0

    for mission in data["missions"]:
        execution = mission.get("execution") or {}
        if execution.get("status") not in {"VERIFIED", "WAITING_DELIVERY"}:
            continue

        processed += 1
        pr_url = find_pr_url(mission)
        delivery = execution.setdefault("delivery", {})

        if not pr_url:
            delivery.update({
                "status": "WAITING_FOR_REAL_PR",
                "checked_at": now(),
                "note": "Aucune URL de Pull Request réelle trouvée dans le rapport Manus. Aucune livraison n'est déclarée.",
            })
            execution["status"] = "WAITING_DELIVERY"
            mission["status"] = "WAITING_DELIVERY"
            changed = True
            print(f"[DELIVERY] {mission.get('id')}: aucune PR fournie; en attente.")
            continue

        try:
            result = verify_pr(pr_url)
            delivery.update(result)
            execution["status"] = "DELIVERED"
            mission["status"] = "DELIVERED"
            print(f"[DELIVERY] {mission.get('id')}: PR vérifiée {pr_url}")
        except Exception as exc:
            delivery.update({
                "status": "DELIVERY_CHECK_FAILED",
                "pr_url": pr_url,
                "checked_at": now(),
                "note": str(exc)[:500],
            })
            execution["status"] = "DELIVERY_BLOCKED"
            mission["status"] = "BLOCKED"
            print(f"[DELIVERY] {mission.get('id')}: échec de vérification: {exc}")
        changed = True

    if changed:
        save_db(data)
    print(f"[DELIVERY] {processed} mission(s) traitée(s).")


if __name__ == "__main__":
    main()
