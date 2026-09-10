# Flussi C, D, E, F — Apertura book, Dispositivi, Credenziali, Knowledge Base

```yaml
---
id: FLOWS-CDEF
title: "Flussi C/D/E/F — Apertura bookmaker, Gestione dispositivi, Credenziali, Knowledge Base"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.4 (7d3f1411)
  - Capitolo 21.5 (f5dbf89d)
  - Capitolo 21.6 (bef885ed)
  - Capitolo 21.7 (78506fa5)
use_cases: [C1-C6, D1-D5, E1-E6, F1-F5]
kb_links:
  - P2-APERTURA-SCALETTA-BOOK
  - P2-APERTURA-GUIDA-NUOVO-ID
  - P2-APERTURA-ELENCO-BOOK
  - P8-CREDENZIALI-IGIENE-ACCOUNT
  - P8-CREDENZIALI-BONUS-COMPLEANNO
  - P3-ANTILIMIT-PRINCIPI
  - P6-RICONCILIAZIONE-PROCEDURA
related:
  - docs/flows/use-case-map.md
---
```

---

## Flusso C — Apertura conti bookmaker (P2)

**Attore**: Raffaele, Collaboratore (se live), Sistema (TouchInjector) | **Frequenza**: 20-30 volte per identità

### Precondizioni
- Identità in stato `ACTIVE` o `READY` con telefono assegnato e conti Hype/PayPal funzionanti
- Guida "Apertura nuovo ID" v4 definisce sequenza e vincoli temporali

### Step-by-step
1. **Avvio e check vincoli (C1, C2)**: Raffaele clicca `[Apri Nuovo Book]`, seleziona book. Sistema interroga DB per date apertura book della stessa famiglia.
   - **Vincolo rispettato**: autorizza e procede
   - **Vincolo violato**: "⚠️ Impossibile aprire Lottomatica. Vincolo di famiglia non rispettato (Goldbet aperto il X, mancano 4 giorni)". Operazione abortita.

2. **Preparazione e avvio (C1)**: Sistema estrae dati anagrafici, prepara sequenza navigazione. `[Avvia Automazione]` → Orchestratore assegna task al telefono → TouchInjector apre browser/app del book

3. **Compilazione form e CAPTCHA (C4)**: TouchInjector compila campi via `input_text`. Se CAPTCHA:
   - **2Captcha risolve**: token iniettato, procede
   - **2Captcha fallisce**: task HITL `CAPTCHA_DETECTED` → Raffaele risolve via scrcpy → `[Ho risolto, riprendi]`

4. **Invio documenti e verifica live (C3)**: 5 book richiedono verifica live (Betpassion, Mylotteries, Sportbet, Zonagioco, Giochi24). Sistema mette in pausa → "⚠️ Verifica Live Richiesta" → Raffaele organizza videochiamata → collaboratore mostra documento → `[Verifica Completata]`

5. **Conferma e salvataggio (C5, B6)**: ScreenReader legge schermata conferma → Raffaele inserisce credenziali → Vault crittografa → AppApiClient registra account in Netcap (`Status=APERTO`)

6. **Gestione Bet365 (C6 - Cautela)**: Sistema NON automatizza. "⚠️ Bet365 richiede gestione manuale cautelativa." → Raffaele esegue via scrcpy (manuale) senza `input_text` automatico

### Edge cases
- **Documento rifiutato**: ScreenReader rileva errore → abort → nuove foto
- **Email già in uso**: book blocca → nuova email
- **App crasha**: `NAVIGATION_FAILED` → riavvia o riprova

---

## Flusso D — Gestione dispositivi

**Attore**: Augusto/Enzo | **Frequenza**: scale-up e manutenzione

### D1 — Aggiunta nuovo telefono
1. Dashboard → `[Aggiungi Dispositivo]` → metadati: model, android_version, screen_resolution, sim_iccid, sim_carrier
2. Sceglie Host ADB → `Device(status=AVAILABLE)`, IP assegnato da AP WiFi
3. `[Test Connessione]` → `adb devices` → ✅ o ❌

### D2 — Assegnazione telefono a identità
- Seleziona dispositivo `AVAILABLE` → associa a `identity_id` → `Device.status=ASSIGNED`

### D4 — Manutenzione (batteria < 15%)
- Alert Telegram a Enzo → collega caricatore al rack → monitoring rileva ricarica → operatività continua

### D3/D5 — Rimozione/Sostituzione
- `[Rimuovi Dispositivo]` → sistema verifica operazioni attive (se sì: alert critico Flusso A7)
- `Device.status=MAINTENANCE` o `REMOVED` → nuovo telefono → `[Sostituisci su Identità]`

### Edge cases
- **IP cambiato (DHCP)**: device OFFLINE → aggiornare IP nel DB o configurare IP statico
- **ADB non autorizzato (post-reset)**: collegare via USB per accettare dialog "Allow USB debugging"

---

## Flusso E — Gestione credenziali e consensi (P8)

**Attore**: Operatore, Collaboratore (per SCA), Augusto | **Frequenza**: ogni 90 giorni (consenso PSD2), rotazione periodica

### E1/E2 — Rinnovo consenso PSD2 AIS per Hype
1. Sistema monitora `Identity.hype_consent_expiry` → 7 giorni prima: alert Telegram
2. Giorno scadenza: `MovementImporter` smette → `CONSENT_EXPIRED`
3. Operatore contatta collaboratore per SCA (2FA)
4. Dashboard → `[Avvia Rinnovo Hype]` → URL OAuth2 GoCardless → collaboratore approva via app
5. GoCardless notifica → `hype_consent_expiry` +90 giorni → MovementImporter riprende

### E3 — Reset password book
- Operatore naviga via scrcpy a pagina reset → nuova password → salvata nel Vault

### E4 — Gestione account bloccato
- `[Blocca Account]` → `Account.status=LIMITATO` o `CHIUSO` → sistema smette operazioni su quel book

### E5 — Rotazione API key Netcap
- Ogni 90 giorni → Augusto genera nuove credenziali → aggiorna configurazione

### E6 — Rotazione master key vault
- Ogni 6 mesi → Augusto ruota chiave master

### Edge cases
- **Collaboratore non risponde**: import Hype bloccato → fallback manuale via OCR
- **Credenziali Netcap scadute**: `401` → alert critico, circuit breaker OPEN, scritture in coda

---

## Flusso F — Gestione knowledge base

**Attore**: Raffaele (domain expert), Augusto (tech lead) | **Frequenza**: continuo + settimanale

### F2 — Aggiunta nuova regola tacita
1. Raffaele scopre edge case durante operazioni (es. "Quando Sisal mostra 'Bonus Pending' in verde, va contato come real")
2. Crea nota in Obsidian cartella `periferia/` → `status: proposta`

### F3 — Validazione regola
1. Settimanalmente Augusto revisiona `periferia/`
2. Se corretta e deterministica → sposta in `active/` → `status: active`
3. Codifica nel RulesEngine (Python) → commit su Git
4. Linter verifica consistenza frontmatter YAML

### F5 — Indicizzazione in ChromaDB
1. Push su Git triggera `RAGSystem.index_document()`
2. ChromaDB cancella vecchi chunk, indicizza nuovi → regola consultabile dal RAG

### F1 — Aggiornamento procedura book
1. Book cambia layout → Raffaele/Augusto apre file KB in Obsidian → aggiorna step → commit → re-indicizzazione

### F4 — Deprecazione regola
- Regola non più valida → Augusto marca come `superseded`

### Edge cases
- **File markdown malformato**: linter fallisce → commit bloccato (pre-commit hooks) o alert → re-indicizzazione salta quel file
