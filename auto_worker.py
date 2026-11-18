#!/usr/bin/env python3
# ============================================================
# AUTO-WORKER AI
# Moteur centralisé de recherche et gestion des tâches rémunérées
# Python 3.10+ | Bibliothèques standard uniquement
# ============================================================

import json
import os
import re
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

APP_NAME = "AUTO-WORKER AI"
PORT = int(os.getenv("PORT", "8080"))
SCAN_INTERVAL = 1800

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "db.json"

GITHUB_API = "https://api.github.com/search/issues"

SEARCH_QUERIES = [
    'is:issue is:open bounty',
    'is:issue is:open reward',
    'is:issue is:open "paid task"',
    'is:issue is:open "$"',
    'is:issue is:open "cash reward"',
]

# ============================================================
# BASE DE DONNÉES JSON
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def load_db():
    if not DB_FILE.exists():
        return {
            "app": APP_NAME,
            "created_at": now(),
            "last_scan": None,
            "status": "INITIALIZED",
            "statistics": {
                "total_found": 0,
                "pending": 0,
                "approved": 0,
                "completed": 0,
                "total_earnings": 0.0
            },
            "opportunities": [],
            "logs": []
        }

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("Attention : db.json illisible. Sauvegarde nécessaire.")
        return {
            "app": APP_NAME,
            "created_at": now(),
            "last_scan": None,
            "status": "RECOVERY",
            "statistics": {},
            "opportunities": [],
            "logs": []
        }


db = load_db()
db_lock = threading.RLock()


def save_db():
    with db_lock:
        temp = DB_FILE.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        os.replace(temp, DB_FILE)


def log(message, level="INFO"):
    entry = {
        "time": now(),
        "level": level,
        "message": message
    }

    with db_lock:
        db.setdefault("logs", []).append(entry)
        db["logs"] = db["logs"][-300:]

    print(f"[{level}] {message}")
    save_db()


# ============================================================
# R1 — SCANNER
# Recherche des opportunités publiques
# ============================================================

def github_search(query):
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "created",
        "order": "desc",
        "per_page": 30
    })

    request = urllib.request.Request(
        f"{GITHUB_API}?{params}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AUTO-WORKER-AI"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("items", [])
    except Exception as e:
        log(f"Erreur GitHub : {e}", "WARNING")
        return []


def scanner():
    log("R1 : démarrage du scanner.")

    found = {}

    for query in SEARCH_QUERIES:
        log(f"Recherche : {query}")

        items = github_search(query)

        for item in items:
            url = item.get("html_url")

            if not url:
                continue

            found[url] = item

        time.sleep(2)

    opportunities = []

    for item in found.values():
        title = item.get("title", "")
        body = item.get("body") or ""

        text = f"{title} {body}".lower()

        reward_match = re.search(
            r"\$\s?(\d+(?:,\d{3})*(?:\.\d+)?)",
            text
        )

        reward = 0.0

        if reward_match:
            try:
                reward = float(reward_match.group(1).replace(",", ""))
            except ValueError:
                pass

        opportunities.append({
            "id": item.get("id"),
            "title": title,
            "description": body[:2500],
            "url": item.get("html_url"),
            "repository": item.get("repository_url"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "reward_estimate_usd": reward,
            "platform": "GitHub",
            "source": "GitHub Issues",
            "status": "NEW",
            "payment_status": "UNVERIFIED",
            "automation_status": "REVIEW_REQUIRED",
            "score": 0
        })

    log(f"R1 : {len(opportunities)} opportunités trouvées.")

    return opportunities


# ============================================================
# R2 — FILTRE
# Évalue la pertinence des opportunités
# ============================================================

def filter_opportunities(items):
    log("R2 : analyse des opportunités.")

    results = []

    for item in items:
        text = (
            item["title"] + " " + item["description"]
        ).lower()

        score = 0

        positive_words = [
            "bounty", "reward", "paid", "payment",
            "cash", "prize", "sponsor", "funded"
        ]

        negative_words = [
            "volunteer", "unpaid", "donation",
            "internship", "closed"
        ]

        for word in positive_words:
            if word in text:
                score += 1

        for word in negative_words:
            if word in text:
                score -= 2

        if item["reward_estimate_usd"] > 0:
            score += 3

        item["score"] = max(0, min(score, 10))

        if item["score"] >= 2:
            item["status"] = "TO_REVIEW"
            results.append(item)

    log(f"R2 : {len(results)} opportunités retenues.")

    return results


# ============================================================
# R3 — ANALYSTE
# Prépare une première analyse
# ============================================================

def analyst(items):
    log("R3 : analyse des missions.")

    for item in items:
        reward = item["reward_estimate_usd"]

        if reward > 0:
            item["analysis"] = (
                "Montant potentiel détecté. "
                "Vérifier les conditions, le financement, "
                "les délais et l'éligibilité."
            )
        else:
            item["analysis"] = (
                "Aucune récompense chiffrée détectée. "
                "Vérification manuelle nécessaire."
            )

        item["estimated_difficulty"] = (
            "UNKNOWN"
        )

        item["profitability"] = (
            "NOT_VERIFIED"
        )

    return items


# ============================================================
# R4 — EXÉCUTEUR
# Prépare les tâches compatibles
# ============================================================

def executor(items):
    log("R4 : vérification des possibilités d'exécution.")

    for item in items:
        # Aucune soumission automatique sans API officielle,
        # autorisation et procédure de validation configurées.
        item["execution"] = {
            "ready": False,
            "mode": "MANUAL_REVIEW",
            "reason": (
                "API officielle et conditions d'exécution "
                "non configurées."
            )
        }

    return items


# ============================================================
# R5 — VÉRIFICATEUR
# ============================================================

def verifier(items):
    log("R5 : vérification des données.")

    for item in items:
        item["verified"] = bool(
            item.get("title")
            and item.get("url")
            and item["url"].startswith("https://")
        )

        if not item["verified"]:
            item["status"] = "INVALID"

    return items


# ============================================================
# R6 — COMPTABLE
# ============================================================

def accounting():
    with db_lock:
        items = db.get("opportunities", [])

        db["statistics"] = {
            "total_found": len(items),
            "pending": sum(
                1 for x in items
                if x.get("status") in ("NEW", "TO_REVIEW")
            ),
            "approved": sum(
                1 for x in items
                if x.get("status") == "APPROVED"
            ),
            "completed": sum(
                1 for x in items
                if x.get("status") == "COMPLETED"
            ),
            "total_earnings": round(
                sum(
                    float(x.get("earned_usd", 0))
                    for x in items
                ), 2
            )
        }

    save_db()
    log("R6 : statistiques mises à jour.")


# ============================================================
# MOTEUR CENTRAL
# ============================================================

scan_lock = threading.Lock()


def run_scan():
    if not scan_lock.acquire(blocking=False):
        log("Un scan est déjà en cours.", "WARNING")
        return

    try:
        with db_lock:
            db["status"] = "SCANNING"

        save_db()

        log("=" * 45)
        log("LANCEMENT DU CYCLE AUTO-WORKER AI")
        log("=" * 45)

        # Agents
        new_items = scanner()
        filtered = filter_opportunities(new_items)
        analyzed = analyst(filtered)
        prepared = executor(analyzed)
        checked = verifier(prepared)

        # Mise à jour sans écraser les statuts déjà modifiés.
        with db_lock:
            existing = {
                x.get("url"): x
                for x in db.get("opportunities", [])
            }

            for item in checked:
                old = existing.get(item["url"])

                if old:
                    for key in (
                        "status",
                        "earned_usd",
                        "execution"
                    ):
                        if key in old:
                            item[key] = old[key]

                existing[item["url"]] = item

            db["opportunities"] = list(existing.values())
            db["last_scan"] = now()
            db["status"] = "RUNNING"

        save_db()
        accounting()

        log("CYCLE TERMINÉ.")

    except Exception as e:
        log(f"Erreur du moteur central : {e}", "ERROR")

        with db_lock:
            db["status"] = "ERROR"

        save_db()

    finally:
        scan_lock.release()


# ============================================================
# TABLEAU DE BORD HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AUTO-WORKER AI</title>
<style>
*{box-sizing:border-box}
body{
 margin:0;background:#0b1020;color:#e8edf8;
 font-family:Arial,sans-serif;padding:20px
}
header{margin-bottom:25px}
h1{color:#55d6be;margin-bottom:5px}
small{color:#91a0bd}
.cards{
 display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));
 gap:12px;margin:20px 0
}
.card{
 background:#151e34;padding:18px;border-radius:12px;
 border:1px solid #263653
}
.value{font-size:27px;font-weight:bold;color:#55d6be}
.label{color:#9eacc7;font-size:13px;margin-top:8px}
button{
 background:#55d6be;color:#08121b;border:0;
 padding:12px 18px;border-radius:8px;font-weight:bold;
 cursor:pointer;margin:5px 5px 15px 0
}
button.secondary{background:#263653;color:white}
.panel{
 background:#151e34;border:1px solid #263653;
 padding:16px;border-radius:12px;margin-bottom:15px
}
table{width:100%;border-collapse:collapse;min-width:650px}
th,td{
 padding:12px;text-align:left;border-bottom:1px solid #293651;
 font-size:13px
}
th{color:#55d6be}
a{color:#55d6be}
.status{color:#ffd166}
.table-wrap{overflow-x:auto}
pre{white-space:pre-wrap;word-break:break-word;color:#9eacc7}
</style>
</head>
<body>

<header>
<h1>AUTO-WORKER AI</h1>
<small>Central Intelligence & Task Management System</small>
</header>

<div class="cards">
 <div class="card"><div class="value" id="total">0</div>
 <div class="label">Opportunités trouvées</div></div>

 <div class="card"><div class="value" id="pending">0</div>
 <div class="label">À examiner</div></div>

 <div class="card"><div class="value" id="approved">0</div>
 <div class="label">Approuvées</div></div>

 <div class="card"><div class="value" id="completed">0</div>
 <div class="label">Terminées</div></div>

 <div class="card"><div class="value" id="earnings">$0</div>
 <div class="label">Gains enregistrés</div></div>
</div>

<div class="panel">
 <h3>Centre de contrôle</h3>
 <p>État : <span id="status" class="status">Chargement...</span></p>
 <p>Dernier scan : <span id="lastscan">-</span></p>
 <button onclick="scan()">LANCER UN SCAN</button>
 <button class="secondary" onclick="refresh()">ACTUALISER</button>
</div>

<div class="panel">
 <h3>Les six agents</h3>
 <p>R1 — Scanner : recherche des opportunités</p>
 <p>R2 — Filtre : sélection des missions pertinentes</p>
 <p>R3 — Analyste : analyse des instructions</p>
 <p>R4 — Exécuteur : préparation de l'exécution</p>
 <p>R5 — Vérificateur : contrôle des résultats</p>
 <p>R6 — Comptable : suivi des gains</p>
</div>

<div class="panel">
 <h3>Opportunités détectées</h3>
 <div class="table-wrap">
 <table>
 <thead>
 <tr>
 <th>Mission</th><th>Récompense détectée</th>
 <th>Score</th><th>État</th><th>Source</th>
 </tr>
 </thead>
 <tbody id="opportunities"></tbody>
 </table>
 </div>
</div>

<div class="panel">
 <h3>Journal du système</h3>
 <pre id="logs">Chargement...</pre>
</div>

<script>
async function refresh(){
 try{
  const r=await fetch('/api/db');
  const d=await r.json();
  const s=d.statistics||{};

  document.getElementById('total').textContent=s.total_found||0;
  document.getElementById('pending').textContent=s.pending||0;
  document.getElementById('approved').textContent=s.approved||0;
  document.getElementById('completed').textContent=s.completed||0;
  document.getElementById('earnings').textContent=
    '$'+(s.total_earnings||0).toFixed(2);

  document.getElementById('status').textContent=d.status||'-';
  document.getElementById('lastscan').textContent=d.last_scan||'-';

  const tbody=document.getElementById('opportunities');
  tbody.innerHTML='';

  for(const x of (d.opportunities||[]).slice().reverse()){
   const tr=document.createElement('tr');

   const title=document.createElement('td');
   const link=document.createElement('a');
   link.href=x.url;
   link.target='_blank';
   link.rel='noopener noreferrer';
   link.textContent=x.title||'Sans titre';
   title.appendChild(link);

   const reward=document.createElement('td');
   reward.textContent='$'+(x.reward_estimate_usd||0);

   const score=document.createElement('td');
   score.textContent=x.score||0;

   const status=document.createElement('td');
   status.textContent=x.status||'-';

   const source=document.createElement('td');
   source.textContent=x.platform||'-';

   [title,reward,score,status,source].forEach(c=>tr.appendChild(c));
   tbody.appendChild(tr);
  }

  document.getElementById('logs').textContent=
   (d.logs||[]).slice(-20).reverse()
    .map(x=>'['+x.level+'] '+x.time+' — '+x.message).join('\n');

 }catch(e){
  document.getElementById('status').textContent='Connexion en attente';
 }
}

async function scan(){
 document.getElementById('status').textContent='SCAN DEMANDÉ';
 await fetch('/api/scan',{method:'POST'});
 setTimeout(refresh,1000);
}

refresh();
setInterval(refresh,5000);
</script>
</body>
</html>
"""


# ============================================================
# SERVEUR WEB
# ============================================================

class DashboardHandler(BaseHTTPRequestHandler):

    def send_content(self, content, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/":
            self.send_content(
                HTML.encode("utf-8"),
                "text/html; charset=utf-8"
            )

        elif self.path == "/api/db":
            with db_lock:
                content = json.dumps(
                    db,
                    ensure_ascii=False
                ).encode("utf-8")

            self.send_content(
                content,
                "application/json; charset=utf-8"
            )

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/scan":
            threading.Thread(
                target=run_scan,
                daemon=True
            ).start()

            self.send_content(
                b'{"success":true}',
                "application/json"
            )
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        return


# ============================================================
# SCAN AUTOMATIQUE
# ============================================================

def automatic_scanner():
    while True:
        time.sleep(SCAN_INTERVAL)
        run_scan()


# ============================================================
# LANCEMENT DU PROJET
# ============================================================

def main():

    print()
    print("=" * 55)
    print("       AUTO-WORKER AI — CENTRAL ENGINE")
    print("=" * 55)

    # Création automatique de db.json
    if not DB_FILE.exists():
        save_db()
        print("[OK] db.json créé automatiquement.")

    log("Initialisation du système.")

    # Premier scan
    threading.Thread(
        target=run_scan,
        daemon=True
    ).start()

    # Scanner périodique
    threading.Thread(
        target=automatic_scanner,
        daemon=True
    ).start()

    # Dashboard
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        DashboardHandler
    )

    print()
    print("[OK] Moteur central lancé.")
    print("[OK] Base de données :", DB_FILE)
    print("[OK] Tableau de bord :", f"http://localhost:{PORT}")
    print("[OK] Actualisation automatique : 5 secondes")
    print("[OK] Nouveau scan : toutes les 30 minutes")
    print()
    print("Appuie sur CTRL+C pour arrêter.")
    print("=" * 55)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du système.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()