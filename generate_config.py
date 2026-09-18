"""Genera config.json con i book reali di ogni ID, leggendo da Netcap."""
import requests
import json
import os
import sys

BASE_URL = "http://37.221.197.184:3001/api"
TOKEN = os.environ.get("NETCAP_SESSION_TOKEN", "")

if not TOKEN:
    print("ERRORE: imposta NETCAP_SESSION_TOKEN")
    print("export NETCAP_SESSION_TOKEN='tuo_token'")
    sys.exit(1)

cookies = {"session_token": TOKEN}

# 1. Leggi saldi-bookmaker (contiene matrix con collaboratore_id -> bookmaker_id -> saldo)
print("Leggo saldi-bookmaker da Netcap...")
r = requests.get(f"{BASE_URL}/saldi-bookmaker", cookies=cookies, timeout=30)
if r.status_code != 200:
    print(f"ERRORE: HTTP {r.status_code}")
    sys.exit(1)

data = r.json()
matrix = data.get("matrix", {})
bookmakers = data.get("bookmakers", [])

# Mappa bookmaker_id -> {nome, is_exchange}
book_map = {}
for b in bookmakers:
    bid = b.get("id")
    if bid is not None:
        book_map[str(bid)] = {
            "name": b.get("nome", ""),
            "is_exchange": b.get("tipo", "") == "exchange"
        }

print(f"Trovati {len(book_map)} bookmaker su Netcap")
print(f"Trovati {len(matrix)} collaboratori con saldi")

# 2. Per ogni collaboratore, trova i book con saldo (anche 0 o negativo)
identities = []
for collab_id in sorted(matrix.keys()):
    books_for_id = matrix[collab_id]
    books = []
    for book_id_str, saldo in books_for_id.items():
        book_id = int(book_id_str)
        book_info = book_map.get(book_id_str)
        if not book_info:
            continue
        # Salta book con saldo esattamente 0 (nessun movimento)
        # Ma tieni i negativi (Betfair può avere saldo negativo)
        if saldo == 0 or abs(saldo) < 0.01:
            continue
        books.append({
            "bookmaker_id": book_id,
            "name": book_info["name"],
            "is_exchange": book_info["is_exchange"]
        })
    
    if not books:
        print(f"  ID {collab_id}: nessun book con saldo significativo, skip")
        continue
    
    # Usa il device_id dal config esistente
    existing_config = json.load(open("orchestrator/config/config.json", "r", encoding="utf-8"))
    device_id = None
    name = f"ID {collab_id}"
    for identity in existing_config.get("identities", []):
        if identity["id"] == collab_id:
            device_id = identity.get("device_id")
            name = identity.get("name", name)
            break
    
    if not device_id:
        print(f"  ID {collab_id}: device_id non trovato nel config, skip")
        continue
    
    identities.append({
        "id": collab_id,
        "name": name,
        "device_id": device_id,
        "books": books
    })
    
    book_names = [b["name"] for b in books]
    print(f"  ID {collab_id} ({name}): {len(books)} book -> {book_names}")

# 3. Genera nuovo config
new_config = {
    "netcap": {
        "base_url": BASE_URL,
        "session_token_env": "NETCAP_SESSION_TOKEN"
    },
    "identities": identities,
    "schedule": {
        "riconciliazione": "09:00",
        "import_movimenti": "10:00"
    }
}

# 4. Salva
output_path = "orchestrator/config/config.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(new_config, f, indent=4, ensure_ascii=False)

print(f"\nConfig salvato: {output_path}")
print(f"Totale identità: {len(identities)}")
print(f"Totale book da riconciliare: {sum(len(i['books']) for i in identities)}")
