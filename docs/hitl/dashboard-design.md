# HITL Dashboard — Mission Control Center

```yaml
---
id: HITL-DASHBOARD
title: "HITL Dashboard — Mission Control Center (v2 Audit)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.9 v2 Audit (553cda6a) — canonica
  - Capitolo 21.9 (79e3ad71) — riferimento mockup
related:
  - docs/flows/use-case-map.md
  - docs/flows/A-daily-operations.md
  - docs/modules/error-handling-system.md
---
```

## 1. Principi di design (Mission Control Approach)

1. **Proattività, non reattività**: l'operatore non deve cercare cosa fare. Il sistema gli mette davanti la prossima azione ottimale.
2. **Context Awareness**: quando l'operatore agisce, deve avere tutto il contesto (saldo, screenshot, quote, T&C) in un solo schermo. Zero navigazioni inutili.
3. **Batching**: se ci sono 10 movimenti da approvare, l'operatore non deve cliccare 10 volte. Si approvano in blocco (HITL Expander).
4. **Live Tracking**: quando l'AI sta lavorando (aprendo un book o piazzando una scommessa), l'operatore deve vedere una "Live Execution Console", non solo uno spinner.

---

## 2. Architettura dell'informazione (Layout ibrido)

### A. Command Center (Home page)

Divisa in 3 colonne dinamiche:

| Colonna | Contenuto | Scopo |
|---|---|---|
| **Sinistra (Critical & Alerts)** | Solo allerte L3 e L2. Operazioni scoperte, device offline critici | Massima priorità visiva |
| **Centro (Action Required — HITL Queue)** | Task che richiedono click umano. Raggruppate per tipo (es. "3 rettifiche saldo", "1 conferma sport") | Azione operativa |
| **Destra (Live Ops Monitor)** | Feed live di cosa sta facendo l'orchestratore in background. ("ID-001: Navigando Betfair...", "ID-005: OCR Saldo Sisal...") | Trasparenza operativa |

### B. Identity Pipeline (Gestione anagrafiche e onboarding)

Vista Kanban (stile Trello) dove le identità sono carte che si muovono:
`Onboarding` → `KYC Pending` → `Ready` → `Active`

Click su carta → pannello di gestione identità (Flusso B e C).

### C. Device Farm Grid (Sistema e hardware)

Griglia visiva di 100 quadranti. Verde = OK, Giallo = Batteria bassa/WiFi debole, Rosso = Offline.
Click sul quadrante → dettaglio dispositivo.

---

## 3. Componenti critici

### Componente 1: HITL Expander (Flussi A1, A3, A10)

Quando l'operatore ha 5 movimenti da approvare, non vuole 5 modali. Usa l'Expander: una card che si espande inline mostrando la tabella.
- **Azione di massa**: `[Approva Tutti]` o selezione singola con checkbox
- **Contesto integrato**: per la rettifica saldo (A1), lo screenshot del telefono è embedded nella card, non in un popup

### Componente 2: War Room Modal (Flussi A7, A9)

Per le emergenze critiche (operazione scoperta), non si usa una card. Si usa un overlay a tutto schermo non chiudabile finché non è risolto.
- Mostra il Runbook step-by-step
- Bottone `[Apri scrcpy]` integrato per prendere il controllo del telefono sostitutivo immediatamente
- Mostra il trace dell'errore (dove si è fermato il Cascade Engine)

### Componente 3: Live Execution Console (Flussi A2, C1, C4)

Quando Raffaele conferma i parametri sport (A2) o avvia l'apertura di un book (C1), il sistema mostra un terminale log-style in tempo reale:

```text
[10:05:12] Avvio navigazione Stanleybet...
[10:05:15] Click su "Scommesse Sportive"
[10:05:18] Inserimento evento 1 (Inter vs Milan)
[10:05:22] ⚠️ CAPTCHA rilevato. Attendo 2Captcha...
[10:05:45] ✅ CAPTCHA risolto. Inserimento stake 100€.
[10:05:48] Conferma scommessa piazzata.
```

Questo dà a Raffaele la certezza che il robot sta lavorando correttamente.

### Componente 4: OCR Side-by-Side (Flusso A4)

Per l'input manuale di valori oscurati o OCR fallito (A4, A11), il modale è diviso in due:
- **Sinistra**: screenshot ad alta risoluzione con tool di zoom
- **Destra**: form di input pre-compilato col valore grezzo (es. "4-5.30"). L'operatore corregge e salva. Tempo richiesto: 3 secondi.

---

## 4. Mockup — Command Center

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  [Logo] MB Automation Command Center      [User: Augusto]  [🔔 2]        │
├──────────────────┬─────────────────────────────────┬─────────────────────┤
│ 🚨 CRITICAL (1)  │ ⚠️ ACTION REQUIRED (HITL)       │ 🤖 LIVE OPS (12)    │
│                  │                                 │                     │
│ ┌──────────────┐ │ ┌─────────────────────────────┐ │ ID-001: ✅ Saldo OK │
│ │ SCOPERTO!    │ │ │ ▼ 3 Rettifiche Saldo        │ │ ID-002: ⏳ Cascade  │
│ │ ID-003 BF    │ │ │   ID-001 Sisal: +€5.00 [✅] │ │ ID-003: 🟡 OCR...   │
│ │ [War Room]   │ │ │   ID-004 SNAI: +€2.00 [✅] │ │ ID-004: ✅ Back OK  │
│ └──────────────┘ │ └─────────────────────────────┘ │ ID-005: ⏳ Betfair  │
│                  │                                 │ ...                 │
│ ⚠️ WARNINGS (2)  │ ┌─────────────────────────────┐ │                     │
│ ┌──────────────┐ │ │ ▼ 1 Conferma Sport          │ │                     │
│ │ Low Battery  │ │ │   Stanleybet | EV: +€12.50  │ │                     │
│ │ Dev-16 (12%) │ │ │   [Vedi Calcoli & Conferma] │ │                     │
│ └──────────────┘ │ └─────────────────────────────┘ │                     │
│                  │                                 │                     │
├──────────────────┴─────────────────────────────────┴─────────────────────┤
│ 📊 TODAY: +€125.50 | Active Ops: 4 | HITL Resolved: 8 | Uptime: 99.9%   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Perché questo design è superiore:**
1. L'operatore vede subito se ci sono incendi (Colonna SX) o se deve solo spuntare task di routine (Colonna Centro)
2. Il monitoraggio live (Colonna DX) rassicura che il sistema sta lavorando, eliminando il "panico da black box"
3. L'azione finanziaria (profitto della giornata) è sempre visibile in basso
