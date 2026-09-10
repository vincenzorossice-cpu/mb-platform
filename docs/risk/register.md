# Risk Register — Rischi e mitigazioni

```yaml
---
id: RISK-REGISTER
title: "Risk Register — Rischi tecnici, operativi e di business"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 19 (Rischi e mitigazioni)
related:
  - docs/roadmap/README.md (Roadmap)
  - docs/kpi/README.md (KPI)
  - ADR-0002 (Screen-first)
  - ADR-0005 (HITL)
---
```

## 1. Rischi tecnici

| Rischio | Probabilità | Impatto | Mitigazione | Residual risk |
|---|---|---|---|---|
| **App di book bloccano uiautomator** | Media | Medio (fallback a OCR) | Test empirico book per book (Fase 0), PaddleOCR come fallback | Basso |
| **ADB input injection rilevato** | Bassa | Alto (perdi identità) | Input humanizzato, test su account di prova, book ad alto rischio rimangono manuali | Medio |
| **Layout book cambia → screen classification si rompe** | Media | Basso (alert HITL) | Monitoring confidence score, re-registrazione template, uiautomator più robusto di template matching | Basso |
| **OCR legge male un numero** | Media | Medio (errore saldo) | Double-check (leggi 2 volte, confronta), HITL per confidence < 0.85 | Basso |
| **Quote Betfair cambiano durante cascata (10-30s ritardo)** | Bassa | Medio | Ricalcolo con quote attuali al momento del piazzamento, HITL se variazione > soglia | Basso |
| **ADB WiFi non scala a 20 dispositivi/host** | Bassa | Alto (architettura da ridisegnare) | Architettura 5 host × 20 dispositivi; se 20 è troppo, passare a 10 host × 10 dispositivi | Basso |
| **Redis failure perde stato cascata** | Bassa | Medio | Recovery da stato reale su Betfair via ScreenReader (vedi error-handling-system.md) | Medio |
| **Netcap non ha API** | Bassa | Alto (tutta la scrittura bloccata) | Reverse engineering endpoint via DevTools; se non esistono, FastAPI layer aggiunto da Augusto | Basso |

---

## 2. Rischi operativi

| Rischio | Probabilità | Impatto | Mitigazione | Residual risk |
|---|---|---|---|---|
| **Device offline prolungato** con operazioni scoperte | Media | Alto (copertura rotta) | Watchdog, alert critical, recovery automatico, runbook operativo | Medio |
| **Consenso PSD2 scade senza rinnovo** | Media | Basso (import bloccato) | Alert 7 giorni prima, operatore contatta collaboratore | Basso |
| **Correlation detection a 100 identità** | Media | Alto (blocco account) | OpSec: IP, device, temporal, behavioral diversity + Correlation Monitor | Medio |
| **Knowledge base incompleta o inaccurata** | Media | Medio | Analisi granularità dopo integrazione, think-aloud per colmare gap, RAG per edge cases | Basso |
| **Team capacity insufficiente** (Augusto part-time) | Alta | Alto (roadmap slitta) | Inserimento dev full-time, priorizzazione rigorosa | Medio |
| **Telegram blocca l'account del bot** | Bassa | Basso (segnali non ricevuti) | Account Telegram di backup, monitoring | Basso |

---

## 3. Rischi di business

| Rischio | Probabilità | Impatto | Mitigazione | Residual risk |
|---|---|---|---|---|
| **Bookmaker blocca account** (per detection o ToS) | Media | Alto (perdita asset) | OpSec, sistema speculare (non toccare produzione), book ad alto rischio manuali | Medio |
| **Bookmaker cambia T&C** di una promo | Alta | Basso (singola operazione) | RAG verifica T&C, HITL per discrepanze, knowledge base aggiornata | Basso |
| **Veicolo societario non definito** | Media | Alto (fase scalata) | Raffaele definisce quadro legale; non bloccante per Fasi 0-3 | Medio |
| **GDPR violation** (dati su Telegram) | Alta (stato attuale) | Alto (sanzioni) | Migrazione a vault, informativa privacy | Basso (post-migrazione) |
| **Capitale bloccato su book** (prelievi lenti) | Media | Medio | Monitoraggio saldi, alert se capitale immobilizzato > soglia | Medio |

---

## 4. Risk matrix

```text
IMPACT
  Alto │  Device offline     │  Correlation       │  ADB detected     │
       │  (scoperto)         │  detection (100 ID) │  (perdi identità) │
       │                     │                     │                   │
  Medio│  OCR error          │  Redis failure      │  Team capacity    │
       │  (saldo sbagliato)  │  (cascade state)    │  (roadmap slitta) │
       │                     │                     │                   │
  Basso│  T&C change         │  Layout change      │  Telegram block   │
       │  (singola op)       │  (HITL alert)        │  (backup account) │
       └─────────────────────┴─────────────────────┴───────────────────
            Bassa                  Media                  Alta         PROBABILITY
```

---

## 5. Top 5 rischi prioritari

| # | Rischio | Perché prioritario | Azione immediata |
|---|---|---|---|
| 1 | Team capacity insufficiente | Augusto part-time, roadmap ambiziosa | Inserire dev full-time subito |
| 2 | Correlation detection a 100 ID | Può bloccare l'intero business | OpSec rigoroso, scale graduale |
| 3 | ADB injection rilevato | Perde asset produttivi (identità) | Test su account di prova prima di production |
| 4 | uiautomator bloccato dai book | Determina accuratezza della lettura (XML vs OCR) | Test empirico Fase 0 |
| 5 | GDPR violation (Telegram) | Sanzionabile legalmente | Migrazione a vault prima di scalare |
