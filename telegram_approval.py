import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")


def run():
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    if not isinstance(db, dict):
        raise RuntimeError("Format db.json invalide.")

    db.setdefault("missions", [])
    db.setdefault("system", {})

    print("[AUTO APPROVAL] Vérification des missions...")

    count = 0

    for mission in db["missions"]:
        analysis = mission.get("analysis", {})

        if (
            mission.get("status") == "WAITING_APPROVAL"
            and analysis.get("suitable") is True
            and analysis.get("risk") == "LOW"
        ):
            mission["status"] = "APPROVED"
            mission["approved_at"] = "AUTO"
            count += 1

            print("[AUTO APPROVED]", mission.get("id"))

    db["updated_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    temp = DB_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    os.replace(temp, DB_FILE)

    print("[AUTO APPROVAL] Missions approuvées :", count)


if __name__ == "__main__":
    run()