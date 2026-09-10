# Architecture Overview

```yaml
---
id: ARCH-OVERVIEW
title: "Architecture Overview — Agentic Screen-First Platform"
status: active
created: 2026-07-23
updated: 2026-07-23
supersedes: null
superseded_by: null
sources:
  - Capitolo 4 (Contratti tra moduli)
  - Capitolo 6 (Error handling e recovery)
  - Capitolo 8 (Risoluzione dei processi)
  - Capitolo 9 (Signal Parser)
kb_links:
  - P5-SEGNALE-LETTURA-INTERPRETAZIONE
  - P5-FONDAMENTI-MATCHED-BETTING
  - P6-RICONCILIAZIONE-PROCEDURA
  - P4-BANKROLL-GESTIONE
---
```

## 1. Visione

Piattaforma di automazione matched betting **screen-first** e **agentico-first**. Ogni modulo è un agente autonomo con confini espliciti, contratti tipizzati, errori strutturati e interfacce HITL (Human-in-the-Loop) dichiarate.

### 1.1 — Principi architetturali

| Principio | Implementazione | Riferimento ADR |
|---|---|---|
| **Screen-first** | Nessun scraping HTML/API ufficiale. Tutto letto via ADB + uiautomator/OCR su telefoni Android fisici | ADR-0002 |
| **Comunicazione sincrona** | Moduli si chiamano via funzioni Python (stesso processo). Redis Streams solo per HITL queue e coordinamento inter-host | ADR-0001 |
| **Tipizzazione forte** | Ogni input/output è dataclass o Pydantic model. Nessun dict generico | ADR-0001 |
| **Errori espliciti** | Ogni modulo restituisce `Result[T]` (Success/Failure + error_code). Nessuna exception silenziosa | ADR-0001 |
| **Idempotenza** | Ogni scrittura ha `idempotency_key`. Duplicate signals → skip | (to be documented in future ADR) |
| **Checkpoint finanziari** | Stato operazione persistente a 6 checkpoint (PENDING → COMPLETED). Punto di non ritorno = BACK_PLACED | (to be documented in future ADR) |
| **HITL come prima classe** | Ogni modulo può richiedere intervento umano via `HITLAlerting`. SLA per severity | (to be documented in future ADR) |
| **Agentico** | Ogni modulo documenta: responsabilità, confini, input, output, errori, fallback, HITL triggers | ADR-0001 |

### 1.2 — Stack tecnologico

| Componente | Tecnologia | Ruolo |
|---|---|---|
| Orchestratore | Python 3.11+ asyncio | Coordinamento moduli, state machine |
| Device interaction | ADB + uiautomator2 | Lettura schermo, tap, navigazione |
| OCR fallback | PaddleOCR | Quando uiautomator bloccato (FLAG_SECURE) |
| LLM | Llama 3 8B (locale) | Estrazione segnali, RAG, edge cases |
| Knowledge base | ChromaDB (vector store) | T&C verification, edge case matching |
| State | Redis (Streams + KV) | Cascade state, device locks, circuit breakers, DLQ, HITL queue |
| Persistence | PostgreSQL (SQLAlchemy) | Operazioni, movimenti, identità, account |
| App gestionale | Netcap (REST API) | Saldi, rettifiche, movimenti, convenzioni segno |
| PSD2 | GoCardless Bank Account Data API | Movimenti Hype |
| Notifiche | WebSocket + Telegram bot | HITL alerting real-time |

---

## 2. Mappa dei moduli (bounded contexts)

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATORE                                │
│  (state machine, checkpoint, operation lifecycle)                   │
└───────┬──────────┬──────────┬──────────┬──────────┬────────────────┘
        │          │          │          │          │
   ┌────▼────┐ ┌───▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐
   │ Signal  │ │Mult-  │ │Cascade │ │ Rules  │ │Movement│
   │ Parser  │ │Engine │ │Engine  │ │Engine  │ │Importer│
   └────┬────┘ └───┬───┘ └───┬────┘ └───┬────┘ └───┬────┘
        │          │         │          │          │
        │     ┌────▼────┐ ┌──▼────┐ ┌───▼────┐     │
        │     │  Touch  │ │Screen│ │  App   │     │
        │     │Injector │ │Reader│ │ApiClient│    │
        │     └─────────┘ └──────┘ └────────┘     │
        │                                              │
   ┌────▼────┐                                    ┌────▼────┐
   │   RAG   │◄────────── consultato da ─────────►│  OpSec  │
   │ System  │    SignalParser, RulesEngine,       │         │
   │(ChromaDB)│   CascadeEngine                    │(Redis)  │
   └─────────┘                                    └─────────┘
```

### Matrice delle dipendenze

> Tutti i contratti sono consolidati in `docs/contracts/README.md` con sezioni anchor per modulo (es. `#screen-reader`, `#touch-injector`, ecc.)

| Modulo | Dipende da | È dipendente da | Sezione contratto |
|---|---|---|---|
| **ScreenReader** | ADB, uiautomator2, PaddleOCR | TouchInjector, CascadeEngine, RulesEngine, MovementImporter | `contracts/README.md#1-screenreader` |
| **TouchInjector** | ScreenReader (coordinate), ADB | CascadeEngine | `contracts/README.md#2-touchinjector` |
| **SignalParser** | RAGSystem (T&C), Llama 3, PaddleOCR (CV) | Orchestratore | `contracts/README.md#3-signalparser` |
| **MultiplierEngine** | Nessuno (pura matematica) | CascadeEngine | `contracts/README.md#4-multiplierengine` |
| **CascadeEngine** | ScreenReader, TouchInjector, MultiplierEngine, AppApiClient, Redis | Orchestratore | `contracts/README.md#5-cascadeengine` |
| **RulesEngine** | Nessuno (pura logica) | Orchestratore (per P6) | `contracts/README.md#6-rulesengine` |
| **RAGSystem** | ChromaDB, Llama 3 | SignalParser, RulesEngine, CascadeEngine | `contracts/README.md#9-ragsystem` |
| **AppApiClient** | Netcap API | RulesEngine, CascadeEngine, MovementImporter | `contracts/README.md#7-appapiclient` |
| **MovementImporter** | GoCardless API, ScreenReader | Orchestratore (per P4/P7) | `contracts/README.md#8-movementimporter` |
| **OpSec** | Redis (counters) | Orchestratore (prima di ogni operazione) | `contracts/README.md#10-opsec` |

---

## 3. Tipizzazione di base — `Result[T]`

Ogni modulo restituisce `Result[T]`. Nessuna exception propagata al chiamante.

```python
from typing import TypeVar, Generic, Optional
from dataclasses import dataclass

T = TypeVar('T')

@dataclass
class Result(Generic[T]):
    success: bool
    data: Optional[T] = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # "DEVICE_OFFLINE", "OCR_LOW_CONFIDENCE", "ADB_TIMEOUT", ecc.

    @classmethod
    def ok(cls, data: T) -> 'Result[T]':
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str, code: str) -> 'Result[T]':
        return cls(success=False, error=error, error_code=code)
```

---

## 4. Modello di failure — 3 livelli

| Livello | Esempio | Impatto | Strategia |
|---|---|---|---|
| **L1 — Transient** | ADB timeout momentaneo, WiFi lag, OCR confidence bassa | Operazione ritardata | Retry automatico con backoff |
| **L2 — Recoverable** | Telefono offline 10 min, app crash, schermata inaspettata | Operazione sospesa, stato preservato | Recovery automatico al rientro del dispositivo |
| **L3 — Critical** | Telefono irraggiungibile >1h, stato Redis perso, back piazzato senza bancata | Rischio finanziario (copertura rotta) | HITL immediato, blocco operazione, alert critico |

### 4.1 — Checkpoint finanziari (state machine operazione)

```text
PENDING          → checkpoint_0: operazione creata, nulla ancora fatto
PARAMS_CONFIRMED → checkpoint_1: parametri confermati da HITL
BACK_PLACED      → checkpoint_2: scommessa back piazzata sul book ← PUNTO DI NON RITORNO
LAY_PLACED       → checkpoint_3: prima bancata piazzata su Betfair
WAITING_RESULT   → checkpoint_4: in attesa del risultato, cascata in gestione
COMPLETED        → checkpoint_5: operazione conclusa, registrata in Netcap
```

**Checkpoint critico = `BACK_PLACED`**: una volta piazzata la scommessa sul book, il sistema **deve** bancare su Betfair. Se fallisce → L3 Critical.

---

## 5. Mappatura processi → moduli

| Processo KB | Nome | Moduli coinvolti (To-Be) | HITL residuo |
|---|---|---|---|
| P1 | Onboarding collaboratore | OCR → TouchInjector (pre-compilazione) → Vault | Verifica dati OCR, KYC materiale, reclutamento |
| P2 | Apertura conti bookmaker | OpSec (vincoli famiglia) → TouchInjector (form) | Verifiche live, Bet365, CAPTCHA complessi |
| P3 | Profilazione/Anti-limit | OpSec (correlation, temporal diversity, device diversity) | Cross-cutting: non un processo separato, ma una preoccupazione trasversale gestita dal modulo OpSec (vedi `docs/modules/core-agents.md#4-opsec`) |
| P4 | Movimentazione fondi | MovementImporter → AppApiClient | Conferma movimenti, approvazione trasferimenti |
| P5 Sport | Esecuzione bonus sport | SignalParser → RAGSystem → MultiplierEngine → TouchInjector → CascadeEngine | Conferma parametri, gestione errori, bonus esclusivi |
| P5 Casinò | Esecuzione bonus casinò | SignalParser → TouchInjector (autoplay) → ScreenReader (monitor) | Decisioni adattive (cambio slot, stop) |
| P6 | Riconciliazione saldi | ScreenReader → AppApiClient → RulesEngine | Rettifiche >50 EUR, OCR <0.85, edge cases |
| P7 | Contabilità e data entry | AppApiClient (auto) → MovementImporter (pre-compile) | Conferma movimenti, classificazione atipici, rinnovo PSD2 |
| P8 | Gestione credenziali | Vault → Alert scadenze → Dashboard | Accessi bloccati, reset password, priorità scadenze |

> **Nota sulla roadmap**: La priorità di attacco (P6 → P7 → P5 Sport, ecc.) è indicativa e basata sull'analisi del ritorno atteso. La roadmap definitiva (`docs/roadmap/`) verrà costruita dopo l'integrazione della knowledge base e i primi test empirici.

---

## 6. KB tracciabilità

Le 140 guide operative della knowledge base "MB Brain" (processi P1-P8) sono la **fonte di verità** per i requisiti di business. Ogni modulo documenta quali guide referenzia:

| Modulo | Guide KB referenziate |
|---|---|
| SignalParser | P5-SEGNALE-LETTURA-INTERPRETAZIONE, P5-FONDAMENTI-TIPOLOGIE-BONUS, P5-FONDAMENTI-REGOLE-SERVIZIO |
| MultiplierEngine | P5-STRUMENTI-MULTIPLICATORE-MULTIODDS, P5-SPORT-MULTIPLE-CASCATA, P5-STRUMENTI-CALCOLATORE-PUNTA-BANCA |
| CascadeEngine | P5-SPORT-MULTIPLE-CASCATA, P5-SPORT-MULTIPLE-EVENTI-CONTEMPORANEI, P5-SPORT-MAGGIORAZIONE-MULTIPLA-APPLICARE |
| RulesEngine | P6-RICONCILIAZIONE-PROCEDURA, P7-APP-CONVENZIONI-SEGNO, P7-APP-CALCOLO-SALDI-PROFITTI |
| MovementImporter | P4-MOVIMENTAZIONE-ID-PER-ID, P7-STRUMENTI-PROFIT-TRACKER |
| AppApiClient | P7-APP-CONVENZIONI-SEGNO, P7-APP-CALCOLO-SALDI-PROFITTI |
| RAGSystem | (tutte le guide, indicizzate in ChromaDB) |
| OpSec | P3-ANTILIMIT-PRINCIPI, P3-PROFILAZIONE-PROVENIENZA-IP, P1-ONBOARDING-DISPOSITIVI |

---

## 7. Navigazione documentazione

| Percorso | Contenuto |
|---|---|
| `docs/architecture/` | Overview, data-model, technical, deployment, testing-security-openitems |
| `docs/adr/` | Architecture Decision Records (0001, 0002) |
| `docs/contracts/` | Schema input/output consolidato per tutti i 10 moduli |
| `docs/modules/` | Documentazione per modulo (README, design, error-handling, core-agents) |
| `docs/flows/` | Flussi operativi (use-case-map, A-H) |
| `docs/hitl/` | HITL dashboard design, design system + user stories |
| `docs/kpi/` | KPI e Definition of Done |
| `docs/risk/` | Risk register |
| `docs/roadmap/` | Roadmap di prodotto |
| `docs/process-resolution/` | As-Is/To-Be/HITL per processo (P1-P8 + nota P3) |
| `ops/runbooks/` | Procedure operative (incident response, recovery) |
