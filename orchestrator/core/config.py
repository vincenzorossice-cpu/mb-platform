import json
from pathlib import Path

DEFAULT_CONFIG_PATH = "orchestrator/config/config.json"


def load_config(config_path: str = None) -> dict:
    """Carica la configurazione da file JSON.

    Struttura attesa:
    {
        "netcap": {
            "base_url": "http://37.221.197.184:3001/api",
            "session_token_env": "NETCAP_SESSION_TOKEN"
        },
        "identities": [
            {
                "id": "01",
                "name": "Raffaele Maiello",
                "device_id": "ZE223GVFJ2",
                "books": [
                    {"bookmaker_id": 6, "name": "BETFAIR", "is_exchange": true},
                    {"bookmaker_id": 42, "name": "SISAL", "is_exchange": false}
                ]
            }
        ],
        "schedule": {
            "riconciliazione": "09:00",
            "import_movimenti": "10:00"
        }
    }
    """
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        return {"identities": [], "netcap": {}, "schedule": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict, config_path: str = None) -> None:
    """Salva la configurazione su file JSON."""
    path = Path(config_path or DEFAULT_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
