#!/usr/bin/env python3
"""Send polished Telegram status cards for verification and delivery events."""
import html
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "db.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}/"


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


def money(value, currency="USD"):
    if value in (None, "", 0, "0"):
        return "Non communiqué"
    try:
        return f"{float(value):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{value} {currency}".strip()


def telegram_send(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants.")
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(API + "sendMessage", data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(str(result))
    return result["result"]["message_id"]


def build_message(mission):
    execution = mission.get("execution") or {}
    delivery = execution.get("delivery") or {}
    verification = execution.get("verification") or {}
    analysis = mission.get("analysis") or {}
    payment = mission.get("payment") or {}

    task_id = html.escape(str(mission.get("id", "N/A")))
    title = html.escape(str(mission.get("title", "Travail sans titre"))[:220])
    status = html.escape(str(mission.get("status") or execution.get("status") or "INCONNU"))
    source = str(mission.get("url") or "")
    pr_url = delivery.get("pr_url") or execution.get("pr_url") or ""
    reward = analysis.get("reward_estimate")
    currency = str(analysis.get("currency") or mission.get("currency") or "USD")

    method = payment.get("method") or mission.get("payment_method") or "Non communiqué"
    pay_url = payment.get("url") or payment.get("link") or mission.get("payment_url") or ""
    revenue_status = payment.get("status") or mission.get("payment_status") or "Non confirmé"

    lines = [
        "⚡ <b>TENA IZY | WORK DELIVERY</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🆔 <b>ID TRAVAIL :</b> <code>{task_id}</code>",
        f"📌 <b>STATUT :</b> {status}",
        f"🔍 <b>VÉRIFICATION :</b> {html.escape(str(verification.get('status') or 'À contrôler'))}",
        f"📦 <b>LIVRAISON :</b> {html.escape(str(delivery.get('status') or 'En attente'))}",
        "",
        "💎 <b>REVENUS DU TRAVAIL</b>",
        f"💰 Montant annoncé : <b>{html.escape(money(reward, currency))}</b>",
        f"💳 Paiement : {html.escape(str(method))}",
        f"🏦 État du paiement : {html.escape(str(revenue_status))}",
        f"🔗 Lien paiement : {html.escape(str(pay_url) if pay_url else 'Non communiqué')}",
        "",
        "📂 <b>PREUVES</b>",
        f"🔗 Mission : {html.escape(source) if source else 'Non disponible'}",
        f"🚀 Pull Request : {html.escape(str(pr_url)) if pr_url else 'Non disponible'}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "<i>Work • Verify • Deliver</i>",
        "<b>TENA IZY — AUTONOMOUS WORK SYSTEM</b>",
    ]
    return "\n".join(lines)


def main():
    data = load_db()
    sent = 0
    changed = False

    for mission in data["missions"]:
        execution = mission.get("execution") or {}
        status = execution.get("status") or mission.get("status")
        if status not in {
            "VERIFIED", "VERIFICATION_BLOCKED", "DELIVERED",
            "DELIVERY_BLOCKED", "WAITING_DELIVERY", "BLOCKED",
        }:
            continue

        notification = mission.setdefault("delivery_telegram", {})
        event_key = str(status) + ":" + str(
            (execution.get("delivery") or {}).get("pr_url") or ""
        )
        if notification.get("last_event_key") == event_key:
            continue

        message_id = telegram_send(build_message(mission))
        notification.update({
            "last_event_key": event_key,
            "message_id": message_id,
            "sent_at": now(),
        })
        sent += 1
        changed = True
        print(f"[TELEGRAM] Notification envoyée pour {mission.get('id')}.")

    if changed:
        save_db(data)
    print(f"[TELEGRAM] {sent} notification(s) envoyée(s).")


if __name__ == "__main__":
    main()
