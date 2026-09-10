# CHANGELOG — MB Platform Documentation

```yaml
---
id: CHANGELOG
title: "CHANGELOG — Documentazione di prodotto MB Platform"
status: active
created: 2026-07-23
updated: 2026-07-23
---
```

## v2.0 — 2026-07-23 (Engineering-Ready)

### Aggiunto
- **24 documenti** riscritti nel formato agentic-first (Docs-as-Code)
- **2 ADR** (Architecture Decision Records): ADR-0001 (agentic-first, sincrono, Result types), ADR-0002 (screen-first, zero API)
- **10 moduli** documentati con Agent README: responsabilità, confini, input/output contracts, error codes, fallback, HITL triggers, metriche, retry policies, KB tracciabilità
- **38 casi di interazione** mappati in 8 categorie (A-H) con step-by-step, edge cases, errori
- **11 entità** del data model documentate con dataclass Python, stati, relazioni, idempotency
- **3 livelli di failure** (L1/L2/L3) con checkpoint finanziari, retry policies, circuit breaker, watchdog, DLQ, matrice di decisione
- **9 KPI di sistema** + KPI per processo (P4, P5 Sport, P5 Casinò, P6, P7) + Definition of Done per 5 fasi
- **19 rischi** (8 tecnici, 6 operativi, 5 business) con risk matrix e top 5 prioritari
- **17 punti aperti** con owner e scadenza per fase
- **4 runbook** operativi (device offline, OCR, cascade error, Netcap down)
- **HITL Dashboard** Mission Control v2 Audit con 4 componenti critici (HITL Expander, War Room, Live Execution Console, OCR Side-by-Side)
- **10 User Stories** in 4 Epics con requisiti non funzionali UI

### Trasformazioni applicate (vs documentazione originale)
1. Frontmatter YAML con `id`, `status`, `sources`, `kb_links`, `depends_on`, `consumed_by`
2. Confini espliciti per ogni modulo (cosa NON fa)
3. HITL come prima classe (trigger documentati con error_code → azione operatore)
4. KB tracciabilità: ogni modulo e processo linka alle guide P1-P8 specifiche
5. Retry policies tabulari per ogni operazione
6. Matrice di decisione errori (errore × contesto → stato × azione × HITL)
7. Contratti tipizzati (dataclass) per ogni output, error codes UPPER_SNAKE_CASE
8. Versioni canoniche identificate per 4 file duplicati (21.1, 21.2, 21.3, 21.9)

### Fonti sorgente
- 22 capitoli dalla KB "MB PRD" (40 file totali, 4 duplicati risolti)
- 140 guide dalla KB "MB Brain" (processi P1-P8)
- Audit esterno (Grok + Fable 5, 2 round)
- Feedback CTO Senior

### Prossima revisione
v2.1 — dopo integrazione knowledge base (~100 file md) e primi test empirici
