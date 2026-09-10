# ADR-0002 — Approccio Screen-First

```yaml
---
id: ADR-0002
title: "Approccio Screen-First: interazione via ADB + uiautomator/OCR, zero API esterne"
status: accepted
date: 2026-07-23
deciders: [Raffaele, Augusto]
supersedes: null
superseded_by: null
sources:
  - Capitolo 1 (Contesto e obiettivi)
  - Capitolo 2 (Approccio screen-first)
related:
  - ADR-0001 (Architettura agentic-first)
---
```

## Contesto

Il sistema deve interagire con sistemi esterni (bookmaker italiani, Betfair Exchange, Hype, PayPal) per leggere saldi, quote, stati scommesse, movimenti, e per piazzare scommesse, navigare, aderire a promozioni.

La domanda architetturale è: **come interagire con questi sistemi?**

## Decisione

**Approccio screen-first**: tutte le interazioni con sistemi esterni avvengono esclusivamente tramite lettura dello schermo (ADB + uiautomator/OCR) e touch injection su dispositivi Android fisici. Nessuna API esterna viene utilizzata.

L'unica API utilizzata è **interna**: l'app gestionale (Netcap) per scrivere dati.

### Eccezioni

| Eccezione | Perché | Cosa |
|---|---|---|
| **Netcap (API interna)** | Sistema interno, nessun rischio detection, nessun ToS | API REST per operazioni, movimenti, rettifiche |
| **GoCardless (PSD2 AIS per Hype)** | Import via schermo fragile, AIS più affidabile | API GoCardless per leggere transazioni Hype (Banca Sella, Institution ID: BANCA_SELLA_SELBIT2B). Non per PayPal. |

## Rationale

| Motivazione | Dettaglio |
|---|---|
| **API dei book assenti o costose** | Non esiste API pubblica per Sisal, Stanleybet, Eurobet, Goldbet, ecc. |
| **Prevenzione detection** | L'uso di API su multipli account crea pattern di traffico rilevabili. Screen-first produce interazioni indistinguibili da un utente umano. |
| **Uniformità** | Un solo pattern (screenshot → leggi → tap) per ogni sistema esterno. Niente adapter per-book. |
| **Costo** | £0 in licenze API. Risparmio ~€60.000+ su scalata a 100 identità (Betfair Live Keys £49.900 vs zero). |
| **Resilienza** | Se un book cambia layout, ScreenReader si adatta (uiautomator legge struttura, OCR legge testo). Se cambia API, l'integrazione si rompe. |

## Cosa NON fa

- **Non usa** WebDriver/Selenium (navigator.webdriver flag rilevabile)
- **Non usa** Appium (rilevabile e lento vs ADB diretto)
- **Non roota** i dispositivi (Play Integrity deve passare)
- **Non usa** VPN/proxy condivisi (IP datacenter flaggati)
- **Non centralizza** account Betfair (ogni telefono ha il proprio, per evitare correlation)

## Tre pilastri dell'architettura

1. **Sistemi deterministici**: percorsi predefiniti, calcoli matematici, regole codificate (MultiplierEngine, RulesEngine, OpSec)
2. **Sistemi generativi/AI**: parsing segnali non strutturati, OCR, RAG per edge cases (SignalParser, ScreenReader, RAGSystem)
3. **Human-in-the-loop**: intervento umano quando il sistema non ha elementi sufficienti per decidere

## Contesto di business (Cap 1)

- Business opera oggi su ~10 identità con ~10 ore/giorno di lavoro manuale
- Obiettivo: scalare da 10 a 100 identità, 80% automazione per processo
- Capitale per scalata: 100-150k EUR, disponibile
- Team: Raffaele (Domain Expert, ~10h/giorno), Augusto (Tech Lead, part-time), Enzo (Head of Ops), Dev full-time da integrare

## Conferme ricevute

| Decisione | Confermato da |
|---|---|
| Approccio screen-first (zero API esterne) | Raffaele |
| API solo interna (Netcap) per scrittura | Raffaele |
| Betfair per-account su ogni telefono | Raffaele |
| Fee Betfair 4,5% | Raffaele |
| Latenza cascata 10-30s accettabile | Raffaele |
| Sistema speculare (non toccare produzione) | Raffaele |
| redroid escluso (solo dispositivi fisici) | Audit esterno |
| Betfair web mobile fully functional | Audit esterno |
| uiautomator funziona senza root | Audit esterno |
| ADB WiFi limite ~20 dispositivi/host | Audit esterno |
