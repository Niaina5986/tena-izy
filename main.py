import os
import json
import requests

from datetime import datetime, timezone


# =====================================================
# CONFIGURATION
# =====================================================

DB_FILE = "db.json"

JOBBI_URL = "https://jobbi.org/gigs.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

MIN_REWARD = float(os.getenv("MIN_REWARD_USD", "5"))

MAX_RESULTS = 200

TIMEOUT = 40


# =====================================================
# DATE
# =====================================================

def now():
    return datetime.now(timezone.utc).isoformat()


# =====================================================
# TELEGRAM
# =====================================================

def notify(message):

    if not TG_TOKEN or not TG_CHAT:
        print("[TELEGRAM]", message)
        return False

    try:

        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",

            json={
                "chat_id": TG_CHAT,
                "text": message,
                "disable_web_page_preview": True
            },

            timeout=20
        )

        response.raise_for_status()

        return True

    except requests.RequestException as e:

        print("Erreur Telegram:", str(e))

        return False


# =====================================================
# NORMALISATION DATABASE
# =====================================================

def normalize_missions(data):

    if isinstance(data, dict):
        return data

    converted = {}

    if isinstance(data, list):

        for index, mission in enumerate(data):

            if not isinstance(mission, dict):
                continue

            mission_id = (
                mission.get("id")
                or mission.get("mission_id")
                or mission.get("url")
                or f"legacy:{index}"
            )

            converted[str(mission_id)] = mission

    return converted


# =====================================================
# CHARGEMENT DATABASE
# =====================================================

def load_db():

    if not os.path.exists(DB_FILE):

        return {
            "missions": {},
            "history": []
        }

    try:

        with open(
            DB_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            db = json.load(f)

        # Ancien format : liste directement dans db.json
        if isinstance(db, list):

            db = {
                "missions": db,
                "history": []
            }

        if not isinstance(db, dict):

            db = {
                "missions": {},
                "history": []
            }

        db["missions"] = normalize_missions(
            db.get("missions", {})
        )

        db.setdefault("history", [])

        return db

    except json.JSONDecodeError as e:

        raise RuntimeError(
            f"Erreur de lecture db.json : {e}"
        )


# =====================================================
# SAUVEGARDE DATABASE
# =====================================================

def save_db(db):

    db["updated_at"] = now()

    temporary = DB_FILE + ".tmp"

    with open(
        temporary,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            db,
            f,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temporary, DB_FILE)


# =====================================================
# EXTRACTION DES DONNEES JOBBI
# =====================================================

def get_jobbi_metadata(item):

    meta = item.get("_jobbi", {})

    if not isinstance(meta, dict):
        meta = {}

    return meta


def get_reward(meta):

    reward = meta.get("reward", {})

    amount = 0
    currency = "UNKNOWN"
    rail = None

    # Format normal : reward est un dictionnaire
    if isinstance(reward, dict):

        amount = reward.get("amount", 0)

        currency = reward.get(
            "currency",
            "UNKNOWN"
        )

        rail = reward.get("rail")

    # Compatibilité : reward est une liste
    elif isinstance(reward, list):

        for element in reward:

            if not isinstance(element, dict):
                continue

            if element.get("amount") is not None:

                amount = element.get("amount")

                currency = element.get(
                    "currency",
                    "UNKNOWN"
                )

                rail = element.get("rail")

                break

    try:

        amount = float(
            str(amount).replace(",", "")
        )

    except (TypeError, ValueError):

        amount = 0

    return amount, str(currency).upper(), rail


# =====================================================
# EXTRACTION DES ELEMENTS JOBBI
# =====================================================

def extract_items(data):

    if isinstance(data, list):

        return data

    if isinstance(data, dict):

        items = data.get("items")

        if isinstance(items, list):
            return items

        items = data.get("gigs")

        if isinstance(items, list):
            return items

        items = data.get("data")

        if isinstance(items, list):
            return items

    raise ValueError(
        "Format de réponse Jobbi non reconnu"
    )


# =====================================================
# RECHERCHE JOBBI
# =====================================================

def fetch_jobbi():

    print("Connexion à Jobbi...")

    response = requests.get(

        JOBBI_URL,

        params={

            "lane": "agent",

            "limit": MAX_RESULTS

        },

        headers={

            "Accept": "application/json",

            "User-Agent": "TENA-IZY-Agent/2.0"

        },

        timeout=TIMEOUT

    )

    response.raise_for_status()

    data = response.json()

    items = extract_items(data)

    valid_items = []

    for item in items:

        if not isinstance(item, dict):
            continue

        valid_items.append(item)

    print(
        "Jobbi :",
        len(valid_items),
        "missions reçues"
    )

    return valid_items


# =====================================================
# VERIFICATION MISSION
# =====================================================

def analyze_mission(item):

    if not isinstance(item, dict):
        return None

    meta = get_jobbi_metadata(item)

    amount, currency, rail = get_reward(meta)

    title = item.get(
        "title",
        "Mission sans titre"
    )

    description = item.get(
        "content_text",
        ""
    )

    url = item.get(
        "url",
        ""
    )

    source = meta.get(
        "source",
        "unknown"
    )

    if not url:
        return None

    if amount <= 0:
        return None

    # Ne pas traiter des récompenses de monnaies
    # inconnues comme des dollars.
    if currency not in ["USD", "USDC", "USDT"]:

        return None

    if amount < MIN_REWARD:
        return None

    payment_state = meta.get(
        "payment_state",
        "unknown"
    )

    payout_grade = meta.get(
        "payout_grade",
        "unknown"
    )

    acceptance = meta.get(
        "acceptance",
        ""
    )

    acceptance_type = meta.get(
        "acceptance_type",
        ""
    )

    claims_open = meta.get(
        "claims_open"
    )

    expires = meta.get(
        "expires"
    )

    mission_id = item.get("id")

    if not mission_id:

        mission_id = (
            source + ":" + url
        )

    return {

        "id": str(mission_id),

        "source": "Jobbi",

        "source_board": source,

        "title": title,

        "description": description,

        "url": url,

        "reward": amount,

        "currency": currency,

        "payment_rail": rail,

        "payment_state": payment_state,

        "payout_grade": payout_grade,

        "acceptance": acceptance,

        "acceptance_type": acceptance_type,

        "claims_open": claims_open,

        "expires": expires,

        "status": "DISCOVERED",

        "manus_task_id": None,

        "manus_status": None,

        "created_at": now(),

        "updated_at": now()

    }


# =====================================================
# AJOUT MISSION
# =====================================================

def save_mission(mission, db):

    if not mission:
        return False

    mission_id = mission["id"]

    missions = db["missions"]

    if mission_id in missions:

        return False

    missions[mission_id] = mission

    return True


# =====================================================
# NOTIFICATION NOUVELLE MISSION
# =====================================================

def notify_new_mission(mission):

    message = (

        "🔎 TENA IZY — NOUVELLE MISSION\n\n"

        f"📌 {mission['title']}\n\n"

        f"💰 Récompense : "
        f"{mission['reward']} "
        f"{mission['currency']}\n"

        f"🏦 Plateforme : "
        f"{mission['source_board']}\n"

        f"💳 Paiement : "
        f"{mission['payment_state']}\n"

        f"⭐ Fiabilité : "
        f"{mission['payout_grade']}\n\n"

        f"📦 Acceptation : "
        f"{mission['acceptance_type']}\n\n"

        f"🔗 {mission['url']}\n\n"

        "Statut : NOUVELLE OPPORTUNITÉ"

    )

    notify(message)


# =====================================================
# TRAITEMENT JOBBI
# =====================================================

def process_jobbi(db):

    print("Traitement Jobbi...")

    items = fetch_jobbi()

    added = 0

    ignored = 0

    for item in items:

        try:

            mission = analyze_mission(item)

            if not mission:

                ignored += 1
                continue

            if save_mission(mission, db):

                added += 1

                print(
                    "Nouvelle mission :",
                    mission["title"]
                )

                notify_new_mission(mission)

            else:

                ignored += 1

        except Exception as e:

            print(
                "Erreur traitement mission:",
                str(e)
            )

    print("Jobbi nouvelles :", added)

    print("Jobbi ignorées :", ignored)

    return added, len(items)


# =====================================================
# GOAT MARKET
# =====================================================

def fetch_goat():

    print(
        "GOAT suspendu : "
        "certificat HTTPS précédemment expiré."
    )

    # Ne pas désactiver SSL.
    # Réactiver seulement après vérification
    # du certificat du serveur.

    return []


# =====================================================
# PROGRAMME PRINCIPAL
# =====================================================

def main():

    print("=" * 50)

    print("TENA IZY V2 — RECHERCHE DES MISSIONS")

    print("=" * 50)

    db = load_db()

    initial_count = len(
        db["missions"]
    )

    print(
        "Missions déjà enregistrées :",
        initial_count
    )

    jobbi_added = 0
    jobbi_received = 0

    goat_added = 0

    # JOBBI
    try:

        jobbi_added, jobbi_received = process_jobbi(db)

    except Exception as e:

        print(
            "Erreur Jobbi :",
            str(e)
        )

        notify(
            "⚠️ TENA IZY — ERREUR JOBBI\n\n"
            + str(e)[:1000]
        )

    # GOAT
    try:

        goat_items = fetch_goat()

        goat_added = len(goat_items)

    except Exception as e:

        print(
            "Erreur GOAT :",
            str(e)
        )

    # SAUVEGARDE
    save_db(db)

    total = len(
        db["missions"]
    )

    new_total = (
        jobbi_added + goat_added
    )

    # RAPPORT
    report = (

        "📊 TENA IZY — RAPPORT DE RECHERCHE\n\n"

        f"🔎 Jobbi reçues : {jobbi_received}\n"

        f"🆕 Nouvelles missions : {jobbi_added}\n"

        f"📁 Total enregistré : {total}\n\n"

        "🌐 Sources :\n"

        "✅ Jobbi : recherche effectuée\n"

        "⏸️ GOAT : temporairement suspendu\n\n"

        f"📊 Nouvelles opportunités : {new_total}"

    )

    print(report)

    notify(report)

    print("=" * 50)

    print("RECHERCHE TERMINEE")

    print("=" * 50)


# =====================================================
# DEMARRAGE
# =====================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            "ERREUR FATALE :",
            str(e)
        )

        notify(
            "🚨 TENA IZY — ERREUR FATALE\n\n"
            + str(e)[:1000]
        )

        raise