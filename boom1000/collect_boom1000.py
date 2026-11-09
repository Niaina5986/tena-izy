import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import websocket

APP_ID = "1089"
URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"
COUNT = int(os.getenv("TICK_COUNT", "100000"))


def main():
    print("Connexion à Deriv...")
    ws = websocket.create_connection(URL, timeout=30)

    try:
        ws.send(json.dumps({
            "active_symbols": "brief",
            "product_type": "basic"
        }))

        while True:
            data = json.loads(ws.recv())
            if "error" in data:
                raise RuntimeError(data["error"]["message"])
            if data.get("msg_type") == "active_symbols":
                break

        symbols = data.get("active_symbols", [])
        matches = [
            s for s in symbols
            if "boom 1000" in s.get("display_name", "").lower()
        ]

        if not matches:
            raise RuntimeError("Symbole Boom 1000 introuvable.")

        symbol = matches[0]["symbol"]
        print("Marché trouvé :", symbol)

        ws.send(json.dumps({
            "ticks_history": symbol,
            "end": "latest",
            "count": COUNT,
            "style": "ticks"
        }))

        while True:
            result = json.loads(ws.recv())
            if "error" in result:
                raise RuntimeError(result["error"]["message"])
            if result.get("msg_type") == "history":
                break

        history = result["history"]

        with open("boom1000_ticks.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["epoch", "datetime_utc", "price"])

            for epoch, price in zip(
                history["times"], history["prices"]
            ):
                date = datetime.fromtimestamp(
                    int(epoch), timezone.utc
                ).isoformat()
                writer.writerow([epoch, date, price])

        print("Ticks collectés :", len(history["prices"]))

    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERREUR :", e)
        sys.exit(1)
