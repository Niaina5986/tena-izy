import os
import json
import requests
from datetime import datetime, timezone

DB_FILE = "db.json"

JOBBI_URL = "https://jobbi.org/gigs.json?lane=agent&limit=200"
GOAT_URL = "https://goatmarket.com/offers.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

MIN_REWARD = float(os.getenv("MIN_REWARD_USD", "5"))


def now():
    return datetime.now(timezone.utc).isoformat()


def load_db():
    if not os.path.exists(DB_FILE):
        return {"missions": {}, "history": []}

    with open(DB_FILE, encoding="utf-8") as f:
        db = json.load(f)

    db.setdefault("missions", {})
    db.setdefault("history", [])

    return db


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def notify(message):
    if not TG_TOKEN or not TG_CHAT:
        print(message)
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=20
        ).raise_for_status()

    except Exception as e:
        print("Telegram:", e)


def fetch_jobbi():
    r = requests.get(JOBBI_URL, timeout=40)
    r.raise_for_status()

    data = r.json()
    return data.get("items", [])


def fetch_goat():
    r = requests.get(GOAT_URL, timeout=40)
    r.raise_for_status()

    data = r.json()

    return data.get("auftraege", [])


def save_jobbi(item, db):
    meta = item.get("_jobbi", {})
    reward = meta.get("reward") or {}

    amount = reward.get("amount")
    currency = reward.get("currency", "UNKNOWN")

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return

    if amount <= 0:
        return

    if currency == "USD" and amount < MIN_REWARD:
        return

    mission_id = "jobbi:" + str(item.get("id"))

    if mission_id in db["missions"]:
        return

    mission = {
        "id": mission_id,
        "source": "Jobbi",
        "source_board": meta.get("source"),
        "title": item.get("title", ""),
        "description": item.get("content_text", ""),
        "url": item.get("url", ""),
        "reward": amount,
        "currency": currency,
        "payment_state": meta.get("payment_state"),
        "payout_grade": meta.get("payout_grade"),
        "acceptance": meta.get("acceptance"),
        "acceptance_type": meta.get("acceptance_type"),
        "claims_open": meta.get("claims_open"),
        "status": "DISCOVERED",
        "manus_task_id": None,
        "created_at": now()
    }

    db["missions"][mission_id] = mission

    notify(
        "🔎 TENA IZY — NOUVELLE MISSION\n\n"
        f"📌 {mission['title']}\n"
        f"💰 {amount} {currency}\n"
        f"🏦 Source : {mission['source_board']}\n"
        f"📊 Paiement : {mission['payment_state']}\n"
        f"🔗 {mission['url']}\n\n"
        "Statut : À analyser"
    )


def save_goat(item, db):
    mission_id = "goat:" + str(item.get("seite", ""))

    if mission_id == "goat:" or mission_id in db["missions"]:
        return

    amount = item.get("betrag_usd")

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return

    if amount < MIN_REWARD:
        return

    mission = {
        "id": mission_id,
        "source": "GOAT",
        "source_board": item.get("plattform"),
        "title": item.get("titel", ""),
        "description": item.get("beschreibung", ""),
        "url": item.get("url", ""),
        "reward": amount,
        "currency": "USD",
        "status": "DISCOVERED",
        "manus_task_id": None,
        "created_at": now()
    }

    db["missions"][mission_id] = mission

    notify(
        "🔎 TENA IZY — MISSION GOAT\n\n"
        f"📌 {mission['title']}\n"
        f"💰 ${amount}\n"
        f"🏦 Plateforme : {mission['source_board']}\n"
        f"🔗 {mission['url']}\n\n"
        "Statut : Éligibilité à vérifier"
    )


def main():
    print("TENA IZY — RECHERCHE")

    db = load_db()
    before = len(db["missions"])

    try:
        for item in fetch_jobbi():
            save_jobbi(item, db)
    except Exception as e:
        print("Jobbi:", e)
        notify("⚠️ Erreur de recherche Jobbi : " + str(e))

    try:
        for item in fetch_goat():
            save_goat(item, db)
    except Exception as e:
        print("GOAT:", e)
        notify("⚠️ Erreur de recherche GOAT : " + str(e))

    save_db(db)

    new_count = len(db["missions"]) - before

    print("Nouvelles missions :", new_count)

    notify(
        "📊 TENA IZY — RAPPORT DE RECHERCHE\n\n"
        f"🔎 Nouvelles missions : {new_count}\n"
        f"📁 Total enregistré : {len(db['missions'])}"
    )


if __name__ == "__main__":
    main()