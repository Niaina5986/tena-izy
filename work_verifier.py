import os
import re
import json
import uuid
import base64
import shutil
import tempfile
import subprocess
import requests

from datetime import datetime, timezone

DB_FILE = "db.json"
API = "https://api.github.com"

GH_PAT = (
    os.getenv("GH_PAT")
    or os.getenv("GITHUB_TOKEN")
)


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    print(f"[TENA IZY DELIVERY] {message}")


def get_headers():
    token = (
        os.getenv("GH_PAT")
        or os.getenv("GITHUB_TOKEN")
    )

    if not token:
        raise RuntimeError(
            "GH_PAT ou GITHUB_TOKEN manquant. "
            "Configure le secret GH_PAT dans GitHub Actions."
        )

    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def load_db():
    if not os.path.exists(DB_FILE):
        raise FileNotFoundError(
            "Le fichier db.json est introuvable."
        )

    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )


def get_missions(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if isinstance(data.get("missions"), list):
            return data["missions"]

        if isinstance(data.get("opportunities"), list):
            return data["opportunities"]

    raise ValueError(
        "Structure db.json non reconnue."
    )


def get_execution(mission):
    if not isinstance(mission.get("execution"), dict):
        mission["execution"] = {}

    return mission["execution"]


def get_delivery(mission):
    execution = get_execution(mission)

    if not isinstance(execution.get("delivery"), dict):
        execution["delivery"] = {}

    return execution["delivery"]


def get_repo_and_issue(url):
    match = re.search(
        r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)",
        str(url)
    )

    if not match:
        raise ValueError(
            "URL GitHub de mission invalide."
        )

    owner, repo, number = match.groups()

    return (
        owner,
        repo.replace(".git", ""),
        int(number)
    )


def github_get(url):
    response = requests.get(
        url,
        headers=get_headers(),
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def github_post(url, payload):
    response = requests.post(
        url,
        headers=get_headers(),
        json=payload,
        timeout=30
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub API erreur {response.status_code}: "
            f"{response.text[:1000]}"
        )

    return response.json()


def extract_patch(mission):
    execution = get_execution(mission)

    patch = execution.get("patch")

    if patch:
        return patch.strip()

    result = execution.get("result", "")

    if isinstance(result, dict):
        patch = result.get("patch")

        if patch:
            return patch.strip()

        result = json.dumps(
            result,
            ensure_ascii=False
        )

    if not isinstance(result, str):
        return None

    patterns = [
        r"```diff\s*(.*?)```",
        r"```patch\s*(.*?)```"
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            result,
            re.DOTALL | re.IGNORECASE
        )

        if match:
            candidate = match.group(1).strip()

            if "--- " in candidate and "+++ " in candidate:
                return candidate

    if (
        result.lstrip().startswith("diff --git")
        or (
            result.lstrip().startswith("--- ")
            and "\n+++ " in result
        )
    ):
        return result.strip()

    return None


def check_mission(owner, repo, issue_number):
    repo_data = github_get(
        f"{API}/repos/{owner}/{repo}"
    )

    if repo_data.get("archived"):
        raise RuntimeError(
            "Dépôt archivé."
        )

    permissions = repo_data.get("permissions") or {}

    if not permissions.get("push"):
        raise RuntimeError(
            "GH_PAT ne possède pas les droits d'écriture "
            "sur ce dépôt."
        )

    issue = github_get(
        f"{API}/repos/{owner}/{repo}/issues/{issue_number}"
    )

    labels = [
        label.get("name", "").lower()
        for label in issue.get("labels", [])
    ]

    forbidden_labels = [
        "bug-me-not",
        "autofix-declined",
        "no-ai",
        "ai-prohibited",
        "human-only"
    ]

    for label in labels:
        if any(word in label for word in forbidden_labels):
            raise RuntimeError(
                f"Mission exclue : label {label}"
            )

    text = (
        issue.get("title", "") + " " +
        issue.get("body", "")
    ).lower()

    forbidden_phrases = [
        "ai-generated submissions are not allowed",
        "no ai-generated",
        "human-only submission",
        "must be completed manually",
        "do not use ai"
    ]

    for phrase in forbidden_phrases:
        if phrase in text:
            raise RuntimeError(
                "Mission nécessitant une intervention humaine."
            )

    return repo_data, issue


def run_git(command, cwd=None, env=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Commande Git échouée: {' '.join(command)}\n"
            f"{result.stderr[-3000:]}"
        )

    return result.stdout.strip()


def create_delivery(mission):
    execution = get_execution(mission)
    delivery = get_delivery(mission)

    mission_id = str(
        mission.get("id", "unknown")
    )

    if delivery.get("pr_url"):
        log(
            f"PR déjà créée pour {mission_id}"
        )
        return

    patch = extract_patch(mission)

    if not patch:
        delivery["status"] = "WAITING_FOR_MANUS_PATCH"
        delivery["updated_at"] = now()

        mission["status"] = "WAITING_DELIVERY"
        execution["status"] = "WAITING_DELIVERY"

        log(
            f"{mission_id}: aucun patch fourni par Manus."
        )
        return

    source_url = (
        mission.get("url")
        or mission.get("source_url")
        or ""
    )

    owner, repo, issue_number = get_repo_and_issue(
        source_url
    )

    repo_data, issue = check_mission(
        owner,
        repo,
        issue_number
    )

    default_branch = repo_data["default_branch"]

    branch = (
        "tena-izy/"
        + re.sub(
            r"[^a-zA-Z0-9_-]",
            "-",
            mission_id
        )
        + "-"
        + uuid.uuid4().hex[:6]
    )

    temp_dir = tempfile.mkdtemp(
        prefix="tena-izy-"
    )

    try:
        token = (
            os.getenv("GH_PAT")
            or os.getenv("GITHUB_TOKEN")
        )

        auth = base64.b64encode(
            f"x-access-token:{token}".encode()
        ).decode()

        git_env = os.environ.copy()
        git_env["GIT_TERMINAL_PROMPT"] = "0"

        git_config = [
            "git",
            "-c",
            f"http.extraheader=AUTHORIZATION: basic {auth}"
        ]

        clone_url = (
            f"https://github.com/{owner}/{repo}.git"
        )

        run_git(
            git_config + [
                "clone",
                "--depth",
                "1",
                "--branch",
                default_branch,
                clone_url,
                temp_dir
            ],
            env=git_env
        )

        clean_env = os.environ.copy()

        clean_env.pop("GH_PAT", None)
        clean_env.pop("GITHUB_TOKEN", None)
        clean_env.pop("GITHUB_ACCESS_TOKEN", None)

        run_git(
            [
                "git",
                "checkout",
                "-b",
                branch
            ],
            cwd=temp_dir,
            env=clean_env
        )

        patch_file = os.path.join(
            temp_dir,
            "tena_izy.patch"
        )

        with open(
            patch_file,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(patch)
            f.write("\n")

        run_git(
            [
                "git",
                "apply",
                "--check",
                patch_file
            ],
            cwd=temp_dir,
            env=clean_env
        )

        run_git(
            [
                "git",
                "apply",
                patch_file
            ],
            cwd=temp_dir,
            env=clean_env
        )

        os.remove(patch_file)

        changes = run_git(
            [
                "git",
                "status",
                "--porcelain"
            ],
            cwd=temp_dir,
            env=clean_env
        )

        if not changes:
            raise RuntimeError(
                "Le patch ne produit aucune modification."
            )

        run_git(
            [
                "git",
                "config",
                "user.name",
                "TENA IZY"
            ],
            cwd=temp_dir,
            env=clean_env
        )

        run_git(
            [
                "git",
                "config",
                "user.email",
                "tena-izy[bot]@users.noreply.github.com"
            ],
            cwd=temp_dir,
            env=clean_env
        )

        run_git(
            ["git", "add", "-A"],
            cwd=temp_dir,
            env=clean_env
        )

        run_git(
            [
                "git",
                "commit",
                "-m",
                f"Automated solution for issue #{issue_number}"
            ],
            cwd=temp_dir,
            env=clean_env
        )

        run_git(
            git_config + [
                "push",
                "origin",
                branch
            ],
            cwd=temp_dir,
            env=git_env
        )

        pr = github_post(
            f"{API}/repos/{owner}/{repo}/pulls",
            {
                "title": (
                    "Automated solution: "
                    + issue.get("title", "Task")
                ),
                "head": branch,
                "base": default_branch,
                "body": (
                    "## TENA IZY - Automated delivery\n\n"
                    f"Related issue: #{issue_number}\n\n"
                    "This is an automated draft Pull Request.\n\n"
                    "Please review the changes and run the "
                    "repository's required tests before merging.\n\n"
                    "No automatic merge has been performed."
                ),
                "draft": True
            }
        )

        delivery["status"] = "PR_CREATED"
        delivery["branch"] = branch
        delivery["pr_url"] = pr["html_url"]
        delivery["pr_number"] = pr["number"]
        delivery["created_at"] = now()

        execution["delivery"] = delivery
        execution["status"] = "WAITING_DELIVERY"

        mission["status"] = "WAITING_DELIVERY"

        log(
            f"Pull Request créée : {pr['html_url']}"
        )

    finally:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


def main():
    if not (
        os.getenv("GH_PAT")
        or os.getenv("GITHUB_TOKEN")
    ):
        raise RuntimeError(
            "GH_PAT ou GITHUB_TOKEN manquant. "
            "Ajoute le secret GH_PAT dans GitHub Actions."
        )

    data = load_db()
    missions = get_missions(data)

    for mission in missions:
        execution = get_execution(mission)

        mission_status = str(
            mission.get("status", "")
        ).upper()

        execution_status = str(
            execution.get("status", "")
        ).upper()

        if (
            mission_status == "VERIFIED"
            or execution_status == "VERIFIED"
        ):
            try:
                create_delivery(mission)

            except Exception as error:
                delivery = get_delivery(mission)

                delivery["status"] = "DELIVERY_ERROR"
                delivery["error"] = str(error)
                delivery["updated_at"] = now()

                execution["status"] = "DELIVERY_BLOCKED"
                mission["status"] = "DELIVERY_BLOCKED"

                log(
                    f"Erreur mission "
                    f"{mission.get('id')}: {error}"
                )

    save_db(data)

    log("Traitement terminé.")


if __name__ == "__main__":
    main()