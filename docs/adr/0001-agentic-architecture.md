# ADR-0001 — Architettura agentic-first con comunicazione sincrona e Result types

```yaml
---
id: ADR-0001
title: "Architettura agentic-first con comunicazione sincrona e Result types"
status: accepted
date: 2026-07-23
deciders: [ prodotto, ingegneria ]
supersedes: null
superseded_by: null
sources:
  - Capitolo 4 (Contratti tra moduli, §4.1)
---
```

## Contesto

La piattaforma automatizza operazioni di matched betting su telefoni Android fisici via ADB. I moduli devono coordinarsi per: parsare segnali, calcolare puntate, piazzare scommesse, gestire cascate sportive, riconciliare saldi, registrare dati.

La domanda architetturale è: **come comunicano i moduli tra loro?**

## Decisione

1. **Comunicazione sincrona**: i moduli si chiamano direttamente via funzioni Python nello stesso processo. Nessuna coda di messaggi tra moduli.

2. **Redis Streams** si usa esclusivamente per:
   - HITL queue (alert verso operatori)
   - Coordinamento inter-host (device locks, circuit breaker state)
   - Dead letter queue
   - Coda di scritture differite a Netcap (quando CB è OPEN)

3. **Tipizzazione forte**: ogni input e output è `dataclass` o `Pydantic model`. Nessun `dict` generico.

4. **Errori espliciti**: ogni modulo restituisce `Result[T]` con `success`, `data`, `error`, `error_code`. Nessuna exception silenziosa.

5. **Agentico**: ogni modulo è documentato come agente autonomo con:
   - Responsabilità (cosa fa)
   - Confini (cosa NON fa)
   - Input contract (cosa riceve)
   - Output contract (cosa produce)
   - Error codes (cosa può fallire e come)
   - Fallback logic (cosa fa quando il percorso primario fallisce)
   - HITL triggers (quando richiede intervento umano)

## Rationale

- **Sincrono vs asincrono (message queue)**: il sistema opera su dispositivi fisici con stato reale (schermo, app aperta). La latenza di una message queue introdurrebbe race conditions sullo stato del dispositivo. La chiamata diretta garantisce che il chiamante sa immediatamente se l'operazione è riuscita.

- **Result type vs exceptions**: le operazioni finanziarie non possono propagare exception non gestite. `Result[T]` costringe il chiamante a gestire esplicitamente il caso di fallimento. Gli `error_code` permettono routing deterministico degli errori (retry, HITL, skip, DLQ).

- **Agentico**: ogni modulo deve essere sviluppabile e testabile in isolamento. I contratti tipizzati permettono mock deterministici. La documentazione agent-ready permette a un LLM di comprendere e estendere il modulo.

## Consequenze

### Positive
- Sviluppo e test per modulo in isolamento
- Tracciabilità completa degli errori (ogni fallimento ha un `error_code`)
- Nessuna infrastruttura message broker tra moduli (ridotta complessità operativa)
- LLM può comprendere e generare codice per un modulo leggendo la sua documentazione

### Negative
- Un crash del processo orchestratore blocca tutti i moduli (mitigato da: checkpoint persistenti, recovery automatico al restart, Redis state preservation)
- Scaling orizzontale limitato (tutto in un processo) — accettabile perché il collo di bottiglia è il numero di telefoni fisici, non la CPU
- Accoppiamento temporale tra moduli (il chiamante attende il risultato) — accettabile perché le operazioni sono intrinsecamente sequenziali (leggi schermo → decidi → tap → verifica)

## Alternative considerate

| Alternativa | Perché scartata |
|---|---|
| **Message queue (RabbitMQ/Kafka) tra moduli** | Latenza introdurrebbe race condition su stato dispositivo. Complessità operativa non giustificata. |
| **Microservices con REST/gRPC** | Overhead di rete per operazioni che sono chiamate di funzione. Servizi allocati su stessa macchina. |
| **Exceptions invece di Result** | Permettono fallimenti silenziosi (exception non catturata). Non costringono il chiamante a gestire il caso errore. |
| **Dict generici come input/output** | Nessun controllo sui tipi. Difficile mockare nei test. LLM non può inferire struttura. |

## Referenze

- ADR-0002 — Screen-first (ADB + uiautomator/OCR)
- ADR-0003 — Idempotency via idempotency_key
- ADR-0004 — Checkpoint finanziari (state machine operazione)
- ADR-0005 — HITL come prima classe
