# Architettura Tecnica

```yaml
---
id: ARCH-TECHNICAL
title: "Architettura Tecnica — 3 layer fisici + 1 logico, Modular Monolith"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 5 (Architettura tecnica)
related:
  - ADR-0001 (Architettura agentic-first)
  - ADR-0002 (Screen-first)
  - docs/architecture/deployment.md
---
```

## 1. Panoramica — 3 layer fisici + 1 logico

```text
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATORE CENTRALE                      │
│  Python 3.11+ — Modular Monolith                             │
│  FastAPI (API interna + HITL Dashboard)                      │
│  Redis (state + coordination)                                 │
│  PostgreSQL (persistent data)                                │
│  GPU: Llama 3 8B + Whisper large-v3                          │
│  Moduli: ScreenReader, TouchInjector, SignalParser,           │
│  CascadeEngine, MultiplierEngine, RulesEngine,               │
│  RAGSystem, AppApiClient, MovementImporter, OpSec            │
└──────────────────┬──────────────────────────────────────────┘
                   │ HTTPS / REST
     ┌─────────────┼─────────────┐
     ↓             ↓             ↓
┌─────────┐  ┌─────────┐  ┌─────────┐  (5 host)
│ HOST 1  │  │ HOST 2  │  │ HOST 3  │
│ ADB WiFi│  │ ADB WiFi│  │ ADB WiFi│
│ 20 ph.  │  │ 20 ph.  │  │ 20 ph.  │
└─────────┘  └─────────┘  └─────────┘
```

## 2. Stack software

| Componente | Tecnologia | Versione |
|---|---|---|
| OS | Ubuntu Server LTS | 24.04 |
| Python | 3.11+ | 3.12 |
| API framework | FastAPI | 0.110+ |
| ASGI server | Uvicorn | 2.x |
| Database | PostgreSQL | 16 |
| Cache/State | Redis | 7.x |
| AI — LLM | Llama 3 8B (Ollama) | 3.1/3.2 |
| AI — ASR | faster-whisper | large-v3 |
| Vector DB | ChromaDB | 0.5+ |
| OCR | PaddleOCR | PP-OCRv4 |
| ADB | Android Platform Tools | latest |
| Process manager | systemd | — |
| Reverse proxy | Nginx | — |

## 3. Struttura del codice (Modular Monolith)

```text
orchestrator/
├── main.py
├── config/{settings.py, logging.py}
├── core/{orchestrator.py, device_manager.py, hitl_dashboard.py, result.py, errors.py}
├── modules/
│   ├── screen_reader/{interface.py, uiautomator.py, ocr.py, classifier.py}
│   ├── touch_injector/{interface.py, adb_input.py, navigation.py}
│   ├── signal_parser/{interface.py, llama_client.py}
│   ├── cascade_engine/{interface.py, state_machine.py, recovery.py}
│   ├── multiplier_engine/{interface.py, calculator.py}
│   ├── rules_engine/{interface.py, reconciliation.py}
│   ├── rag_system/{interface.py, chroma_client.py, indexer.py}
│   ├── app_api_client/{interface.py, netcap_client.py}
│   ├── movement_importer/{interface.py, gocardless.py, paypal_ocr.py}
│   └── opsec/{interface.py, scheduler.py, correlation.py}
├── api/{routes_hitl.py, routes_devices.py, routes_health.py}
├── db/{models.py, migrations/, redis_client.py}
└── tests/{unit/, integration/, e2e/}
```

Regola: ogni modulo espone `interface.py` con classi astratte (ABC). L'orchestratore importa le interfacce, non le implementazioni. Nessun modulo importa direttamente un altro modulo.

## 4. Host Agent

Ogni host (mini-PC) esegue un Host Agent — servizio Python lightweight con API REST:

| Endpoint | Funzione |
|---|---|
| `GET /health` | Health check, conta dispositivi ADB |
| `POST /screencap` | Screenshot da dispositivo |
| `POST /uiautomator` | Estrae XML UI hierarchy |
| `POST /input_tap` | Tap humanizzato (jitter ±5px, delay 0.3-1.5s) |
| `POST /input_text` | Input testo |
| `GET /devices` | Lista dispositivi connessi |

Perché Host Agent invece di ADB diretto: l'orchestratore non gestisce 100 connessioni ADB WiFi (limite ~20 per processo). L'Host Agent esegue ADB localmente.

## 5. Topologia di rete

- Switch Gigabit centrale
- Ogni host ha il proprio AP WiFi (20 telefoni per AP, isolati)
- I telefoni non hanno accesso a Internet via WiFi — connessione dati è via SIM
- WiFi è solo per ADB over WiFi (controllo)

## 6. Deploy

| Ambiente | Scopo | Come |
|---|---|---|
| DEV | Sviluppo locale | `python main.py` |
| TEST | Sistema speculare (3-5 telefoni, identità di test) | `git pull && systemctl restart orchestrator` |
| PROD | 100 identità | Script di deploy controllato |

## 7. Backup e DR

| Cosa | Frequenza | Retention |
|---|---|---|
| PostgreSQL | Giornaliero (`pg_dump`) | 30 giorni |
| Redis RDB | Ogni 6 ore | 7 giorni |
| ChromaDB | Settimanale | 4 settimane |
| Codice | Continuo (Git) | Infinito |
| Screenshot HITL | 30 giorni poi cleanup | 30 giorni |

Regola critica: il stato finanziario (PostgreSQL) è l'unica cosa che non può essere persa. Redis è volatile e recuperabile.

## 8. Monitoring hardware

`DeviceManager.heartbeat_all()` ogni 60 secondi:
- Verifica ogni telefono via ADB
- Legge batteria (`adb shell dumpsys battery`)
- Aggiorna `Device.battery_level` e `Device.last_seen`
- Se offline >5 min → alert
- Temperature mini-PC: se CPU > 80°C → alert
