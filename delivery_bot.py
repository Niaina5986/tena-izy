import os
import json
import time
import base64
import re
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "delivery_inbox"
DATABASE = ROOT / "livraison.json"

TOKEN = os.getenv("GH_PAT")
API = "https://api.github.com"

CHECK_INTERVAL = 20
MAX_WAIT_SECONDS = 12 * 60

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

if not TOKEN:
    raise SystemExit("ERREUR : GH_PAT absent.")


def load_db():
    if not DATABASE.exists():
        return {"version": 1, "deliveries": {}}

    try:
        return json.loads(
            DATABASE.read_text(encoding="utf-8")
        )
    except Exception:
        raise RuntimeError(
            "livraison.json illisible. Arrêt par sécurité."
        )


def save_db(db):
    temp = DATABASE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(db, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    temp.replace(DATABASE)


def api(method, endpoint, **kwargs):
    response = requests.request(
        method,
        API + endpoint,
        headers=HEADERS,
        timeout=45,
        **kwargs
    )

    if not response.ok:
        raise RuntimeError(
            f"GitHub API {response.status_code}: "
            f"{response.text[:1000]}"
        )

    if response.status_code == 204:
        return {}

    if not response.content:
        return {}

    return response.json()


def safe_path(value):
    path = Path(value)

    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Chemin interdit : {value}")

    if "\\" in value:
        raise ValueError(f"Chemin non autorisé : {value}")

    return path.as_posix()


def get_files(job_dir, manifest):
    source = job_dir / manifest.get("source_dir", "files")
    source = source.resolve()

    if not source.is_relative_to(job_dir.resolve()):
        raise ValueError("Dossier source interdit.")

    if not source.is_dir():
        raise ValueError("Dossier files introuvable.")

    result = []

    for file in source.rglob("*"):

        if file.is_symlink():
            raise ValueError("Liens symboliques interdits.")

        if not file.is_file():
            continue

        relative = file.relative_to(source).as_posix()
        relative = safe_path(relative)

        if relative.lower() in (
            "db.json",
            "livraison.json"
        ):
            raise ValueError(
                f"Modification interdite : {relative}"
            )

        content = file.read_bytes()

        if len(content) > 10 * 1024 * 1024:
            raise ValueError(
                f"Fichier trop volumineux : {relative}"
            )

        result.append({
            "path": relative,
            "content": content
        })

    if not result:
        raise ValueError("Aucun fichier à livrer.")

    if len(result) > 100:
        raise ValueError("Limite de 100 fichiers dépassée.")

    return result


def create_delivery(job_dir, manifest, db):

    job_id = manifest["job_id"]

    if job_id in db["deliveries"]:
        print("Mission déjà enregistrée :", job_id)
        return

    if manifest.get("ready") is not True:
        raise ValueError("Mission non déclarée prête.")

    repo = manifest["repository"]

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        repo
    ):
        raise ValueError("Nom de dépôt invalide.")

    base = manifest.get("base_branch", "main")
    title = manifest.get("title", f"Delivery: {job_id}")

    files = get_files(job_dir, manifest)

    api("GET", f"/repos/{repo}")

    base_ref = api(
        "GET",
        f"/repos/{repo}/git/ref/heads/{base}"
    )

    base_sha = base_ref["object"]["sha"]

    base_commit = api(
        "GET",
        f"/repos/{repo}/git/commits/{base_sha}"
    )

    blobs = []

    for file in files:

        encoded = base64.b64encode(
            file["content"]
        ).decode()

        blob = api(
            "POST",
            f"/repos/{repo}/git/blobs",
            json={
                "content": encoded,
                "encoding": "base64"
            }
        )

        blobs.append({
            "path": file["path"],
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"]
        })

    for deleted in manifest.get("delete_paths", []):

        deleted = safe_path(deleted)

        if deleted.lower() in (
            "db.json",
            "livraison.json"
        ):
            raise ValueError(
                "Suppression d'un fichier protégé interdite."
            )

        blobs.append({
            "path": deleted,
            "mode": "100644",
            "type": "blob",
            "sha": None
        })

    tree = api(
        "POST",
        f"/repos/{repo}/git/trees",
        json={
            "base_tree": base_commit["tree"]["sha"],
            "tree": blobs
        }
    )

    commit = api(
        "POST",
        f"/repos/{repo}/git/commits",
        json={
            "message": f"Delivery: {title}",
            "tree": tree["sha"],
            "parents": [base_sha]
        }
    )

    branch = (
        "delivery/"
        + re.sub(
            r"[^a-zA-Z0-9-]",
            "-",
            job_id.lower()
        )[:45]
    )

    api(
        "POST",
        f"/repos/{repo}/git/refs",
        json={
            "ref": f"refs/heads/{branch}",
            "sha": commit["sha"]
        }
    )

    pr = api(
        "POST",
        f"/repos/{repo}/pulls",
        json={
            "title": title,
            "head": branch,
            "base": base,
            "body": (
                manifest.get(
                    "description",
                    "Automated delivery by R7."
                )
                + "\n\n"
                + f"Source agent: "
                + f"{manifest.get('source', 'Unknown')}"
                + "\n"
                + f"Delivery ID: {job_id}"
            )
        }
    )

    db["deliveries"][job_id] = {
        "repository": repo,
        "branch": branch,
        "base": base,
        "commit": commit["sha"],
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "status": "PR_CREATED",
        "created_at": time.time(),
        "source": manifest.get("source", "Unknown")
    }

    save_db(db)

    print("LIVRAISON CRÉÉE :", pr["html_url"])


def check_validation(repo, sha):

    checks = api(
        "GET",
        f"/repos/{repo}/commits/{sha}/check-runs"
    )

    statuses = api(
        "GET",
        f"/repos/{repo}/commits/{sha}/status"
    )

    check_runs = checks.get("check_runs", [])
    status_contexts = statuses.get("statuses", [])

    if not check_runs and not status_contexts:
        return "PENDING"

    for check in check_runs:

        if check.get("status") != "completed":
            return "PENDING"

        if check.get("conclusion") != "success":
            return "FAILED"

    for status in status_contexts:

        if status.get("state") != "success":
            return "FAILED"

    return "SUCCESS"


def merge_pull_request(repo, number, sha):

    pr = api(
        "GET",
        f"/repos/{repo}/pulls/{number}"
    )

    if pr.get("state") != "open":
        return False

    if pr.get("draft"):
        print("PR en brouillon : fusion interdite.")
        return False

    if pr.get("merged"):
        return True

    if pr.get("head", {}).get("sha") != sha:
        print("Le code a changé : nouvelle vérification nécessaire.")
        return False

    if pr.get("mergeable") is False:
        print("PR non fusionnable.")
        return False

    result = api(
        "PUT",
        f"/repos/{repo}/pulls/{number}/merge",
        json={
            "sha": sha,
            "merge_method": "squash"
        }
    )

    if result.get("merged") is True:
        print("FUSION AUTOMATIQUE RÉUSSIE :", result.get("html_url"))
        return True

    return False


def monitor_deliveries(db):

    deadline = time.time() + MAX_WAIT_SECONDS

    active = {
        job_id: item
        for job_id, item in db["deliveries"].items()
        if item.get("status") not in (
            "MERGED",
            "CLOSED",
            "CHECKS_FAILED"
        )
    }

    while active and time.time() < deadline:

        for job_id, item in list(active.items()):

            repo = item["repository"]
            number = item["pr_number"]
            sha = item["commit"]

            try:

                pr = api(
                    "GET",
                    f"/repos/{repo}/pulls/{number}"
                )

                if pr.get("merged"):
                    item["status"] = "MERGED"
                    active.pop(job_id)
                    continue

                if pr.get("state") == "closed":
                    item["status"] = "CLOSED"
                    active.pop(job_id)
                    continue

                validation = check_validation(repo, sha)

                if validation == "PENDING":
                    item["status"] = "WAITING_CHECKS"
                    continue

                if validation == "FAILED":
                    item["status"] = "CHECKS_FAILED"
                    active.pop(job_id)
                    print("ÉCHEC DES CONTRÔLES :", job_id)
                    continue

                if validation == "SUCCESS":

                    item["status"] = "CHECKS_PASSED"
                    save_db(db)

                    merged = merge_pull_request(
                        repo,
                        number,
                        sha
                    )

                    if merged:
                        item["status"] = "MERGED"
                        active.pop(job_id)
                    else:
                        item["status"] = "MERGE_BLOCKED"
                        active.pop(job_id)

            except Exception as error:

                print(
                    "Erreur surveillance",
                    job_id,
                    error
                )

                item["status"] = "MERGE_BLOCKED"
                active.pop(job_id)

            save_db(db)

        if active:
            time.sleep(CHECK_INTERVAL)

    for job_id, item in active.items():

        if item.get("status") == "WAITING_CHECKS":
            item["status"] = "WAITING_CHECKS_TIMEOUT"

            print(
                "Temps d'attente dépassé :",
                job_id
            )

    save_db(db)


def main():

    INBOX.mkdir(exist_ok=True)

    db = load_db()

    for job_dir in INBOX.iterdir():

        if not job_dir.is_dir():
            continue

        manifest_file = job_dir / "delivery.json"

        if not manifest_file.exists():
            continue

        try:

            manifest = json.loads(
                manifest_file.read_text(
                    encoding="utf-8"
                )
            )

            create_delivery(
                job_dir,
                manifest,
                db
            )

        except Exception as error:

            print(
                "LIVRAISON EN ERREUR :",
                job_dir.name,
                error
            )

    monitor_deliveries(db)


if __name__ == "__main__":
    main()