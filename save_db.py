import json
import os
import subprocess
import sys
import time
import tempfile
import shutil

DB_FILE = "db.json"
MAX_RETRIES = 5


def git(*args):
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr)

    return result.stdout.strip()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_missions(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("missions", "opportunities"):
            if isinstance(data.get(key), list):
                return data[key]

    raise ValueError("Structure db.json inconnue")


def mission_id(mission):
    return str(mission.get("id", ""))


def merge_missions(remote, local):
    """
    Fusionne les missions par ID.
    Les champs locaux modifiés sont conservés.
    Les missions distantes absentes localement sont ajoutées.
    """

    remote_list = get_missions(remote)
    local_list = get_missions(local)

    merged = {
        mission_id(m): m
        for m in remote_list
        if mission_id(m)
    }

    for mission in local_list:
        mid = mission_id(mission)

        if not mid:
            continue

        if mid not in merged:
            merged[mid] = mission
            continue

        old = merged[mid]

        # Fusion des champs principaux.
        for key, value in mission.items():
            if isinstance(value, dict) and isinstance(
                old.get(key), dict
            ):
                old[key] = {
                    **old[key],
                    **value
                }
            else:
                old[key] = value

    if isinstance(remote, list):
        return list(merged.values())

    result = remote.copy()

    if "missions" in result:
        result["missions"] = list(merged.values())
    elif "opportunities" in result:
        result["opportunities"] = list(merged.values())

    return result


def main():
    if not os.path.exists(DB_FILE):
        print("db.json introuvable")
        sys.exit(1)

    local_data = load_json(DB_FILE)

    for attempt in range(1, MAX_RETRIES + 1):

        print(f"Tentative {attempt}/{MAX_RETRIES}")

        try:
            # Ne jamais continuer un ancien rebase bloqué.
            subprocess.run(
                ["git", "rebase", "--abort"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Sauvegarde du travail local.
            backup = tempfile.mktemp(
                prefix="tena-db-",
                suffix=".json"
            )

            shutil.copy2(DB_FILE, backup)

            # Récupération du dernier état distant.
            git("fetch", "origin")

            branch = git(
                "rev-parse",
                "--abbrev-ref",
                "HEAD"
            )

            remote_branch = f"origin/{branch}"

            # Lire la base distante.
            remote_text = git(
                "show",
                f"{remote_branch}:{DB_FILE}"
            )

            remote_data = json.loads(remote_text)

            # Fusionner les données.
            merged = merge_missions(
                remote_data,
                local_data
            )

            save_json(DB_FILE, merged)

            # Vérifier le JSON.
            load_json(DB_FILE)

            git("add", DB_FILE)

            status = git("status", "--porcelain")

            if not status:
                print("Aucune modification à enregistrer.")
                os.remove(backup)
                return

            git(
                "commit",
                "-m",
                "TENA IZY: synchronize mission database"
            )

            try:
                git("push", "origin", branch)

                print("Synchronisation réussie.")
                os.remove(backup)
                return

            except RuntimeError:
                print("Push refusé, nouvelle synchronisation...")

                # Conserver la version fusionnée.
                local_data = load_json(DB_FILE)

                # Annuler le commit local non envoyé.
                git("reset", "--soft", "HEAD~1")

                os.remove(backup)

        except Exception as error:
            print("Erreur:", error)

        time.sleep(2 * attempt)

    print("ÉCHEC : synchronisation impossible après 5 essais.")
    sys.exit(1)


if __name__ == "__main__":
    main()