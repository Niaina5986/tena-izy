import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import websocket

APP_ID = os.getenv("DERIV_APP_ID", "1089")
URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

COUNT = int(os.getenv("TICK_COUNT", "100000"))
TIMEOUT = 40
MAX_RETRIES = 5


def connect():
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Connexion Deriv : tentative {attempt}/{MAX_RETRIES}")

            ws = websocket.create_connection(
                URL,
                timeout=TIMEOUT,
                enable_multithread=True
            )

            print("Connexion établie !")
            return ws

        except Exception as error:
            print(f"Échec connexion : {error}")

            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    "Impossible de joindre Deriv après 5 tentatives."
                )

            delay = attempt * 10
            print(f"Nouvel essai dans {delay} secondes...")
            time.sleep(delay)


def request(ws, payload, expected_type):
    ws.send(json.dumps(payload))
    deadline = time.time() + TIMEOUT

    while time.time() < deadline:
        message = json.loads(ws.recv())

        if "error" in message:
            error = message["error"]
            raise RuntimeError(
                f"{error.get('code')} : {error.get('message')}"
            )

        if message.get("msg_type") == expected_type:
            return message

    raise TimeoutError("Délai de réponse Deriv dépassé.")


def main():
    print("=== COLLECTEUR BOOM 1000 ===")
    print("Mode : lecture seule")
    print("Ticks demandés :", COUNT)

    ws = connect()

    try:
        response = request(
            ws,
            {
                "active_symbols": "brief",
                "product_type": "basic"
            },
            "active_symbols"
        )

        symbols = response.get("active_symbols", [])

        matches = [
            s for s in symbols
            if "boom 1000" in s.get("display_name", "").lower()
        ]

        if not matches:
            raise RuntimeError("Boom 1000 introuvable.")

        symbol = matches[0]["symbol"]

        print("Marché :", matches[0]["display_name"])
        print("Symbole :", symbol)

        response = request(
            ws,
            {
                "ticks_history": symbol,
                "end": "latest",
                "count": COUNT,
                "style": "ticks"
            },
            "history"
        )

        history = response.get("history", {})
        prices = history.get("prices", [])
        times = history.get("times", [])

        if not prices or not times:
            raise RuntimeError("Aucune donnée reçue.")

        count = min(len(prices), len(times))

        with open(
            "boom1000_ticks.csv",
            "w",
            newline="",
            encoding="utf-8"
        ) as file:

            writer = csv.writer(file)
            writer.writerow(["epoch", "datetime_utc", "price"])

            for epoch, price in zip(times[:count], prices[:count]):
                date = datetime.fromtimestamp(
                    int(epoch),
                    timezone.utc
                ).isoformat()

                writer.writerow([epoch, date, price])

        print("Collecte terminée :", count, "ticks")
        print("Fichier : boom1000_ticks.csv")

    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ÉCHEC :", error, file=sys.stderr)
        sys.exit(1)
