# Moduli: Multiplier Engine, Cascade Engine, RAG System, OpSec

```yaml
---
id: MOD-CORE-AGENTS
title: "Moduli core — Multiplier Engine, Cascade Engine, RAG System, OpSec (Agent README)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 10 (Multiplier Engine)
  - Capitolo 11 (Cascade Engine)
  - Capitolo 12 (RAG System e Knowledge Base)
  - Capitolo 13 (OpSec)
kb_links:
  - P5-SPORT-MULTIPLE-CASCATA
  - P5-STRUMENTI-MULTIPLICATORE-MULTIODDS
  - P5-SPORT-MULTIPLE-EVENTI-CONTEMPORANEI
  - P5-STRUMENTI-CALCOLATORE-PUNTA-BANCA
  - P3-ANTILIMIT-PRINCIPI
  - P3-PROFILAZIONE-PROVENIENZA-IP
  - P1-ONBOARDING-DISPOSITIVI
---
```

---

## 1. MultiplierEngine — Agent README

### Responsabilità
Calcola puntate, responsabilità e scenari per multiple a cascata (sport) o singole (casinò). Sostituisce NinjaBet MultiOdds (€19,98/mese, no API).

### Confini
- Pura matematica, nessuna dipendenza esterna
- Non piazza scommesse, non legge schermo, non accede a DB

### Interfaccia

```python
class MultiplierEngine:
    def calculate_cascade(self, back_stake, back_odds, lay_odds, commission=0.045, strategy="equal_profit") -> Result[CascadeCalculation]
    def calculate_single(self, back_stake, back_odds, lay_odds, commission=0.045) -> Result[SingleCalculation]
    def recalculate_from_index(self, back_stake, back_odds, lay_odds, commission, from_index) -> Result[CascadeCalculation]
```

### Strategia equal-profit

Garantisce lo stesso profitto in ogni scenario (vittoria al primo, secondo, terzo evento, o multipla vinta sul book). Calcolo sequenziale a ritroso.

```python
lay_stake = remaining_to_cover / (1 - commission)
liability = lay_stake * (lay_odds - 1)
profit_if_won = lay_stake * (1 - commission)
```

### Esempio numerico
```python
back_stake=100, back_odds=[2.0, 2.0, 2.0], lay_odds=[1.95, 1.95, 1.95], commission=0.045
# combined_odds = 8.0, potential_win = 700.0
# Evento 1: lay_stake=733.0, liability=696.35, profit_if_won=700.0
# Evento 2: stesso (equal-profit)
# Evento 3: stesso
# total_liability=696.35, is_valid=True
```

### Arrotondamenti
Betfair: 2 decimali. Book italiani: 1 decimale. Errore cumulativo max ~€0.03 su tripla. Warning se > €0.50.

### Validazione vs NinjaBet
Via Playwright su 10 segnali reali. Tolleranza: ±€0.50 per scenario.

### Error codes
`INVALID_ODDS` (quote <= 1.0) → HITL | `NEGATIVE_PROFIT` → skip | `ROUNDING_ERROR` → warning | `VALIDATION_FAILED` → investigazione

### KB tracciabilità
P5-STRUMENTI-MULTIPLICATORE-MULTIODDS, P5-SPORT-MULTIPLE-CASCATA, P5-STRUMENTI-CALCOLATORE-PUNTA-BANCA

---

## 2. CascadeEngine — Agent README

### Responsabilità
Gestisce la state machine della cascata sportiva: monitora stato eventi su Betfair via ScreenReader, decide quando bancare il prossimo evento, coordina con TouchInjector e MultiplierEngine.

### State machine (16 stati)

```text
pending → event_1_layed → event_1_waiting → event_1_won → completed
                                           → event_1_lost → event_2_layed → ...
event_3_lost → completed (tutti persi = multipla vinta sul book)
* → error → recover → pending
* → hitl_blocked → resume → pending
```

### Flusso operativo
1. `start_cascade`: acquisisci device lock, crea stato in Redis (TTL 48h), piazza prima bancata via TouchInjector, aggiorna stato a `EVENT_1_WAITING`
2. `check_and_advance`: leggi stato scommessa su Betfair via ScreenReader
   - `settled_won` → completa operazione, scrivi in Netcap
   - `settled_lost` → piazza prossima bancata (ricalcola se quote cambiate >0.05)
   - `matched`/`unsettled` → riprova tra 30-60s
3. `recover_cascade`: recovery da Redis state o da stato reale Betfair

### Scheduling
- Eventi `unsettled`: check ogni 30s
- Eventi `matched`: check ogni 60s
- Eventi pre-match lontani: check ogni 5 min
- Max 5 check simultanei

### Betfair web mobile (primario)
Chrome su Android, non app nativa. Play Integrity non richiesto (Chrome passa), DOM accessibile, navigazione via URL e tap.

### Modello per-account
Ogni identità ha il proprio Betfair loggato sul proprio telefono. Niente centralizzazione.

### Error codes
`CASCADE_NOT_FOUND`, `DEVICE_BUSY`, `DEVICE_OFFLINE`, `BET_NOT_FOUND`, `LAY_PLACEMENT_FAILED`, `MAX_RETRIES_EXCEEDED`, `STATE_INCONSISTENT`, `ODDS_NEGATIVE_PROFIT`

### KB tracciabilità
P5-SPORT-MULTIPLE-CASCATA, P5-SPORT-MULTIPLE-EVENTI-CONTEMPORANEI, P5-SPORT-MAGGIORAZIONE-MULTIPLA-APPLICARE

---

## 3. RAGSystem — Agent README

### Responsabilità
Indicizza la knowledge base (ChromaDB) e la rende consultabile semanticamente per edge cases non codificabili deterministicamente nel RulesEngine. Verifica T&C dei segnali.

### Knowledge base
- ~100 file markdown strutturati con frontmatter YAML (id, process, category, bonus_type, book, tags, status)
- Naming: `P{numero}-{categoria}-{book}-{tipo}.md`
- 4 categorie bonus: Benvenuto, Ricorrente, Esclusivo, Rimborso slot

### Cattura knowledge tacita (20% non documentato)
- Think-aloud protocol: 6 sessioni (8-10h totali) con Raffaele
- Trascrizione con Whisper large-v3 (WER 5-7% italiano)
- Estrazione regole con Llama 3: deterministiche, euristiche, adattive

### Classificazione regole
| Tipo | Confidence | Destinazione |
|---|---|---|
| Deterministiche | >= 0.90 | RulesEngine (codice Python) |
| Euristiche | 0.70-0.89 | ChromaDB (RAG) |
| Adattive | requires_hitl=true | ChromaDB + HITL obbligatorio |

### Interfaccia

```python
class RAGSystem:
    def query(self, question, context, process=None, book=None) -> Result[RAGResponse]
    def verify_tc(self, signal: Signal) -> Result[TCVerification]
    def index_document(self, file_path: str) -> Result[bool]
```

### Decision logic
- confidence >= 0.85 e action == "auto_execute" → esegui
- confidence < 0.85 o action == "hitl_confirm" → HITL
- action == "no_match" → HITL con "Nessun caso simile trovato"

### Metriche
| Metrica | Target |
|---|---|
| Documenti indicizzati | ~100 + regole tacite |
| Query latency P95 | < 3s |
| Retrieval accuracy (top 3) | > 80% |
| T&C verification rate | > 80% |
| T&C discrepancy rate | < 15% |
| RAG auto-execute rate | < 10% |
| RAG HITL rate | > 80% |

### KB tracciabilità
Tutte le guide P1-P8 (140 file nella KB "MB Brain")

---

## 4. OpSec — Agent README

### Responsabilità
Monitora e previene correlation detection multi-account. Scheduling temporale delle operazioni.

### Threat model
100 identità reali sugli stessi bookmaker → rischio correlation detection: IP, device fingerprinting, timing patterns, behavioral analysis, shared payment methods, registration patterns.

### Contromisure architetturali

| Dimensione | Misura |
|---|---|
| IP diversity | SIM dati per dispositivo, niente WiFi per traffico book, niente VPN commerciale |
| Device diversity | Modelli/versioni Android/risoluzioni diverse, app rumore, non rootare |
| Registration diversity | Tempi distanziati (guida v4), dati reali, strumenti dedicati, nessun riutilizzo |
| Behavioral diversity | Slot/eventi/importi/orari diversi |

### Temporal diversity scheduling
- Finestra preferita hash-based (stabile per identità): 08:00-19:00
- Jitter: 0-3 ore
- Verifica correlation: se bloccato, shifta di 1-3 ore o domani

### Correlation Monitor

| Soglia | Default |
|---|---|
| MAX_CONCURRENT_SAME_BOOK | 5 identità stesso book stessa ora |
| MAX_SAME_AMOUNT | 8 operazioni stesso importo stesso giorno |
| MAX_HOURLY_OPERATIONS | 15 totali per ora |
| MIN_OPERATION_SPACING | 60s tra operazioni |
| DAILY_MAX_PER_IDENTITY | 10 |

Redis counters con TTL 25h per correlation tracking.

### Error codes
`OPSEC_BLOCKED` (risk=high) → operazione bloccata, HITL_BLOCKED

### Metriche
| Metrica | Target |
|---|---|
| OpSec blocks | < 1% delle operazioni |
| OpSec warnings | < 5% |
| Ops per book per ora | Mai > 5 |
| Ops per identità per giorno P95 | < 8 |
| Spacing P5 | > 60s |

### KB tracciabilità
P3-ANTILIMIT-PRINCIPI, P3-PROFILAZIONE-PROVENIENZA-IP, P1-ONBOARDING-DISPOSITIVI
