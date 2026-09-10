# Design System e User Stories

```yaml
---
id: HITL-DESIGN-SYSTEM
title: "Interfaccia, Design System e User Stories — Mission Control Center"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 22 (Interfaccia e Design System)
  - Capitolo 23 (Requisiti e User Stories)
related:
  - docs/hitl/dashboard-design.md
  - docs/flows/use-case-map.md
---
```

## 1. Principi di Design (Mission Control)

1. **Proattività, non reattività**: l'operatore non cerca cosa fare. Il sistema mette davanti la prossima azione ottimale.
2. **Context Awareness**: tutto il contesto (saldo, screenshot, quote, T&C) in un solo schermo. Zero navigazioni inutili.
3. **Batching**: 10 movimenti da approvare → 1 click, non 10.
4. **Live Tracking**: quando l'AI lavora, l'operatore vede una Live Execution Console, non uno spinner.

## 2. Target Audience

| Ruolo | Usa per | Bisogno chiave |
|---|---|---|
| **Raffaele** (Domain Expert) | Approvare operazioni, valutare T&C, onboarding, casi limite casinò | Contesto visivo chiaro per decidere in pochi secondi |
| **Augusto** (Tech Lead) | Alert critici, failure cascata, debug log/trace | Runbook guidati, trace end-to-end |
| **Enzo** (Head of Ops) | Stato 100 dispositivi, rotazione SIM, rinnovi PSD2 | Vista condensata hardware |

## 3. Architettura dell'informazione (3 colonne + Kanban + Grid)

### A. Command Center (Home — 3 colonne)
- **Sinistra**: Critical & Alerts (L3/L2 only)
- **Centro**: Action Required (HITL Queue, raggruppate per tipo)
- **Destra**: Live Ops Monitor (feed real-time orchestratore)

### B. Identity Pipeline (Kanban)
`Onboarding` → `KYC Pending` → `Ready` → `Active`

### C. Device Farm Grid
Griglia 10×10: Verde=OK, Giallo=Batteria bassa, Rosso=Offline. Click → dettaglio.

## 4. Componenti UI

### Componente 1: HITL Expander
Card collassabile che si espande inline mostrando tabelle. `[Approva Tutti]` o checkbox singoli. Screenshot embedded, non popup.

### Componente 2: War Room Modal
Overlay a tutto schermo non chiudabile. Mostra Runbook step-by-step, bottone `[Apri scrcpy]`, trace dell'errore.

### Componente 3: Live Execution Console
Terminale log-style in tempo reale:
```text
[10:05:12] Avvio navigazione Stanleybet...
[10:05:22] ⚠️ CAPTCHA rilevato. Attendo 2Captcha...
[10:05:45] ✅ CAPTCHA risolto. Inserimento stake 100€.
```

### Componente 4: OCR Side-by-Side
Sinistra: screenshot ad alta risoluzione con zoom. Destra: form con valore grezzo. Tempo: 3 secondi.

### Componente 5: Device Grid Map
Griglia 10×10 di quadranti colorati. Hover: ID, Modello, Batteria, IP. Click: dettaglio.

## 5. User Stories (4 Epics)

### Epic 1: Daily HITL Operations
- **US1.1**: Approva rettifiche saldo in blocco con screenshot embedded
- **US1.2**: Conferma operazioni sport con tabella calcoli e checkbox identità
- **US1.3**: Classifica movimenti atipici con dropdown inline

### Epic 2: Edge Cases & AI Fallback
- **US2.1**: Risoluzione OCR con modale side-by-side (screenshot + input)
- **US2.2**: Discrepanza T&C con opzioni `[Usa Segnale]` `[Usa KB]` `[Apri T&C Ufficiale]`
- **US2.3**: Slot esaurita con statistiche wagering e 3 bottoni: `[Ricarica]` `[Cambia Slot]` `[Chiudi in Perdita]`

### Epic 3: Emergency & Critical Management
- **US3.1**: War Room Modal per operazione scoperta con Runbook e `[Apri scrcpy]`
- **US3.2**: Quota cambiata con calcolo perdita e opzioni `[Piazza a Perdita]` `[Attendi]`

### Epic 4: Onboarding & Book Opening
- **US4.1**: Form a 19 campi con drag&drop documenti → OCR → campi gialli da verificare
- **US4.2**: Apertura book con check vincoli famiglia (tooltip se bloccato) e Live Execution Console

## 6. Requisiti non funzionali UI

| Requisito | Specifica |
|---|---|
| Tecnologia | FastAPI backend + React/Vue o HTMX/Alpine.js per real-time |
| Accessibilità | Usabile da tablet (Raffaele) e desktop (Enzo/Augusto). Bottoni grandi, contrasto alto |
| Performance | HITL queue carica in <1s anche con 100 identità. Paginazione/virtualizzazione |
| Tema | Dark mode preferenziale. Accenti semantici: Verde=OK, Giallo=Warning, Rosso=Critical, Blu=Azione |
