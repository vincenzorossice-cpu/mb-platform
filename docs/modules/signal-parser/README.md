# Module: SignalParser — Agent README

```yaml
---
id: MOD-SIGNAL-PARSER
title: "SignalParser — Telegram Signal to Structured Signal Object"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 4 (§4.4 — interfaccia e error codes)
  - Capitolo 9 (per esteso)
kb_links:
  - P5-SEGNALE-LETTURA-INTERPRETAZIONE
  - P5-FONDAMENTI-TIPOLOGIE-BONUS
  - P5-FONDAMENTI-REGOLE-SERVIZIO
  - P5-ADESIONE-VERIFICA-CAPITALE
depends_on:
  - RAGSystem (per T&C verification)
  - Llama 3 8B (per LLM extraction)
  - PaddleOCR (per emoji CV pipeline)
consumed_by:
  - Orchestratore
contracts:
  - docs/contracts/signal-parser/input-schema.md
  - docs/contracts/signal-parser/output-schema.md
  - docs/contracts/signal-parser/error-codes.md
---
```

## Responsabilità

Riceve un messaggio Telegram grezzo (semi-strutturato con emoji come delimitatori) e lo trasforma in un oggetto `Signal` strutturato, usando una pipeline ibrida regex → LLM → post-processing → T&C verification.

## Confini (cosa NON fa)

- **Non** decide se eseguire l'operazione (compito dell'orchestratore)
- **Non** calcola puntate o responsabilità (compito di `MultiplierEngine`)
- **Non** piazza scommesse (compito di `TouchInjector`)
- **Non** accede a dispositivi Android per il parsing (usa testo già acquisito)
- **Non** verifica la disponibilità di capitale (compito dell'orchestratore, vedi P5-ADESIONE-VERIFICA-CAPITALE)

## Interfaccia

```python
class SignalParser:
    def parse(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        """Parsea un segnale Telegram grezzo in un oggetto Signal strutturato."""

    async def parse_and_verify(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        """Pipeline completa: preprocess → LLM → postprocess → T&C verification → persist."""
```

## Pipeline

```text
[Messaggio Telegram grezzo (con valori CV risolti)]
  ↓
[Step 1: Pre-processing regex] — estrazione campi base via emoji delimitatori
  ↓
[Step 2: LLM extraction] — Llama 3 8B con few-shot prompting estrae JSON strutturato
  ↓
[Step 3: Post-processing] — normalizza book, parse deadline, valida campi, type casting
  ↓
[Step 4: T&C verification] — RAGSystem verifica contro knowledge base (ChromaDB)
  ↓
[Signal object struttato + persistito]
```

## Idempotenza

Il `telegram_message_id` è la chiave di idempotenza. Se un segnale con lo stesso ID è già stato processato → `DUPLICATE_SIGNAL` → skip.

## HITL triggers

| Trigger | Error code | Cosa fa l'operatore |
|---|---|---|
| Book non identificabile | `MISSING_CRITICAL_FIELD` | Compila manualmente il book |
| Scadenza non parseabile | `MISSING_CRITICAL_FIELD` | Inserisce la data manualmente |
| Categoria sport/casino non determinabile | `MISSING_CRITICAL_FIELD` | Classifica manualmente |
| LLM produce JSON malformato (dopo 2 retry) | `LLM_PARSE_FAILED` | Compila il segnale manualmente |
| Valori oscurati (emoji animate non risolvibili) | `OBSCURED` | Inserisce importi/quote manualmente |
| T&C non verificati (discrepanza con KB) | `TC_DISCREPANCY` | Verifica manualmente i T&C del book |

## Metriche

| Metrica | Misurazione | Target |
|---|---|---|
| Parse success rate | `signals_parsed / signals_received` | > 90% |
| LLM parse failures | `Counter('signal_parse_failures', reason=[...])` | < 5% |
| T&C verification rate | `signals_tc_verified / signals_parsed` | > 80% |
| T&C discrepancy rate | `signals_tc_discrepancy / signals_parsed` | < 15% |
| Parse latency | `Histogram('signal_parse_latency_seconds')` | P95 < 10s |
| HITL rate (obscured values) | `signals_hitl_obscured / signals_parsed` | Dipende dal canale |

## Retry policy

| Operazione | Max retries | Backoff | Retry on | Non retry on |
|---|---|---|---|---|
| `LLM extraction` | 2 | 3s, 6s | `LLM_TIMEOUT` | `LLM_PARSE_FAILED`, `MISSING_CRITICAL_FIELD` |
| `T&C verification (RAG)` | 1 | 5s | `RAG_TIMEOUT` | `NO_MATCH`, `TC_DISCREPANCY` |

## KB tracciabilità

| Guida KB | Come è referenziata |
|---|---|
| P5-SEGNALE-LETTURA-INTERPRETAZIONE | Definisce la struttura del segnale e i campi obbligatori |
| P5-FONDAMENTI-TIPOLOGIE-BONUS | Classificazione bonus_type (ricorrente, benvenuto, esclusivo, rimborso_slot) |
| P5-FONDAMENTI-REGOLE-SERVIZIO | Regole di condotta, priorità del segnale, canali di comunicazione |
| P5-ADESIONE-VERIFICA-CAPITALE | Verifica dei T&C (step 1 della guida operativa) |
