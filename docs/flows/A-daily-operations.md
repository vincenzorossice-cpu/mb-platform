# Flusso A — Daily Operations (HITL quotidiano)

```yaml
---
id: FLOW-A-DAILY-OPS
title: "Flusso A — Daily Operations: 11 flussi HITL quotidiani"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.2 (versione canonica 4c2e96cf)
use_cases:
  - A1, A2, A3, A4, A5, A6, A7, A8, A9, A10, A11
kb_links:
  - P5-SEGNALE-LETTURA-INTERPRETAZIONE
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
  - P5-SPORT-MULTIPLE-CASCATA
  - P5-CASINO-SLOT-FONDAMENTI
related:
  - docs/flows/use-case-map.md
  - docs/modules/error-handling-system.md
---
```

## A1 — Conferma rettifica saldo

**Attore**: Operatore | **Frequenza**: 2-5 volte/mese per identità

### Precondizioni
- Sistema ha completato lettura schermo (P6) per un book di un'identità
- `RulesEngine` ha calcolato differenza > €1 (soglia tolleranza)
- `Rectification` in stato `PENDING`

### Step-by-step
1. **Sistema rileva differenza**: ScreenReader legge saldo reale (€45,30), AppApiClient legge saldo atteso da Netcap (€40,30), RulesEngine calcola +€5,00
2. **Notifica asincrona**: HITLAlerting invia Telegram → "🟡 HITL WARNING: ID-001 | Sisal | Rettifica saldo | Differenza: +€5,00"
3. **Operatore apre dashboard**: clicca link Telegram, login, coda HITL pending
4. **Dashboard mostra dettagli**: Identità, Book, Saldo reale (confidence), Saldo atteso, Differenza, Regole applicate, Screenshot, Azioni: `[Approva]` `[Rifiuta]` `[Modifica importo]`
5. **Operatore agisce**:
   - **5a Approva**: AppApiClient scrive in Netcap → card ✅
   - **5b Rifiuta**: modal con motivo → `status=REJECTED` → card ❌
   - **5c Modifica**: edita importo (es. prelievo non registrato) → salva e approva → card ✅

### Errori e edge cases
- **Netcap down**: toast rosso, operazione in coda, circuit breaker OPEN
- **Sessione scaduta**: 401 → "Sessione scaduta, rieffettua login"
- **Rettifica già processata**: "Questa rettifica è già stata processata da [NomeOperatore]"

---

## A2 — Conferma parametri operazione sport

**Attore**: Raffaele | **Frequenza**: ogni operazione sportiva

### Precondizioni
- Segnale sportivo arrivato, SignalParser ha estratto JSON
- RAGSystem ha verificato T&C (o trovato discrepanze)
- MultiplierEngine ha calcolato puntate e responsabilità

### Step-by-step
1. **Parsing segnale**: TelegramAcquirer risolve emoji animate → SignalParser estrae → RAGSystem verifica T&C → MultiplierEngine calcola cascata (equal-profit)
2. **Notifica HITL**: Telegram → "🟡 HITL: Conferma Parametri Sport | Stanleybet | 3 identità pronte | Profitto atteso: +€12,50"
3. **Dashboard mostra**: Book, Tipo, Strategia, Verifica T&C (✅ o ⚠️ discrepanza), Tabella calcolo multipla (Evento/Back Odds/Lay Odds/Lay Stake/Liability/Profit), Checkbox identità target (preselezionate per saldo e OpSec)
4. **Raffaele conferma**: clicca `[Conferma e Piazzamento]`
5. **Esecuzione automatizzata** (per ogni identità, secondo OpSec):
   - TouchInjector.navigate_to (apre app book)
   - TouchInjector.place_back_bet (inserisce eventi, stake, conferma)
   - ScreenReader verifica "Piazzata" nei My Bets
   - TouchInjector apre Betfair web mobile
   - TouchInjector.place_lay_bet (market, odds, stake, conferma)
   - CascadeEngine avvia state machine in Redis
   - UI: righe verdi progressivamente → "ID-001: Back ✅ | Lay ✅ | Cascade Iniziata ⏳"
6. **Chiusura**: task passa in "Operazioni Attive", scritto in Netcap come `WAITING_RESULT`

### Errori e edge cases
- **Saldo insufficiente**: BET_NOT_CONFIRMED, dashboard mostra errore, altre identità continuano
- **Quota cambiata su Betfair**: ricalcolo dinamico, se profitto positivo piazza automaticamente, se negativo → Flusso A9
- **OpSec Block**: se 5 identità simultanee sullo stesso book → blocco correlation, operatore riduce numero o posticipa

---

## A3 — Conferma movimenti importati

**Attore**: Operatore | **Frequenza**: 5-10 volte/giorno

### Step-by-step
1. MovementImporter legge movimenti (cron ogni 30 min o trigger manuale), applica convenzioni di segno, crea `Movement` in stato `PENDING`
2. Notifica batch: "ℹ️ 3 nuovi movimenti per ID-001 (Hype) pronti"
3. Dashboard mostra tabella: Data | Identità | Conto | Descrizione | Importo | Segno | [Modifica]
4. Operatore clicca `[Approva Tutti]` → AppApiClient scrive batch in Netcap → righe verdi

### Errori
- **Movimento non riconosciuto** (A10): descrizione vuota → operatore compila manualmente
- **Errore di segno**: operatore inverte segno prima di approvare
- **Duplicato**: sistema scarta automaticamente (idempotency_key)

---

## A4 — Risoluzione OCR low confidence

**Attore**: Operatore | **Frequenza**: variabile

### Step-by-step
1. ScreenReader tenta saldo su Bet365, uiautomator bloccato, OCR restituisce "€ 4-5.30" con confidence 0.65
2. Task HITL `OCR_LOW_CONFIDENCE` → notifica rossa dashboard (no Telegram per evitare spam)
3. Dashboard mostra: screenshot originale + valore grezzo OCR + campo input
4. Operatore guarda screenshot, digita `45.30`, clicca `[Salva e Continua]`
5. Sistema accetta dato manuale come `BalanceReading(read_method="hitl_manual", confidence=1.0)`

### Edge cases
- **Screenshot sfocato**: `[Richiedi nuovo screenshot]` via ADB
- **Timeout >30 min**: alert Telegram WARNING, riconciliazione bloccata

---

## A5 — Risoluzione CAPTCHA

**Attore**: Operatore | **Frequenza**: raro

### Step-by-step
1. ScreenReader non trova elemento atteso, rileva iframe CAPTCHA. 2Captcha fallisce.
2. Alert Telegram WARNING: "CAPTCHA Rilevato | ID-001 | Stanleybet"
3. Dashboard mostra screenshot live (refresh 5s), istruzioni, `[Apri scrcpy]` e `[Ho risolto, riprendi]`
4. **Modalità A (scrcpy)**: operatore usa mouse sul PC per risolvere CAPTCHA
   **Modalità B (fisica)**: operatore va al rack, risolve con il dito
5. TouchInjector riprende: nuovo `read_screen`, continua da dove era rimasto

### Edge cases
- **CAPTCHA persistente** (3 volte consecutive): operazione abortita, possibile blocco account

---

## A6 — Gestione discrepanza T&C

**Attore**: Raffaele | **Frequenza**: < 15% dei segnali

### Step-by-step
1. RAGSystem confronta Signal.strategy.min_odds (1.8) con KB (2.0) → flag `tc_discrepancies`
2. Dashboard mostra badge giallo "⚠️ T&C Discrepancy" con: Segnale (1.8) vs KB (2.0), fonte KB, opzioni: `[Usa Segnale]` `[Usa T&C]` `[Apri T&C online]` `[Annulla]`
3. Raffaele investiga (apre T&C ufficiale), scopre quota minima è 2.0
4. Clicca `[Usa T&C (2.0)]` → MultiplierEngine ricalcola → opzionalmente aggiorna KB

### Edge cases
- **T&C non trovati nella KB**: RAGSystem non trova documenti → "Verifica manuale richiesta" → operatore inserisce parametri

---

## A7 — Gestione errore cascata (Device Offline)

**Attore**: Raffaele o Augusto | **Frequenza**: raro ma critico (L3)

### Step-by-step
1. DeviceManager conferma telefono offline da >65 min, CascadeEngine ha OP-005 in `BACK_PLACED` (scoperto!)
2. Alert multiplo: Telegram critico + SMS/chiamata ad Augusto/Enzo: "🚨 CRITICAL: OPERAZIONE SCOPERTA"
3. Dashboard mostra schermata rossa: stato `BACK_PLACED`, telefono offline, ultimo check 65 min fa, pulsanti: `[Assegna Sostitutivo]` `[Forza Check]` `[Risolvi Manualmente]`
4. **Se riavviato**: attesa, DeviceManager rileva rientro, CascadeEngine.recover_cascade → verde
   **Se morto (batteria)**: operatore collega caricatore o clicca `[Assegna Sostitutivo]` → DeviceManager migra identità
5. **Peggior caso**: evento già iniziato/finato senza bancata → `[Risolvi Manualmente]` → operatore piazza bancata a mano o registra perdita → `COMPLETED` con profit/perdita reale

### Edge cases
- **Rientro falso positivo**: telefono risponde ma app chiusa → TouchInjector ri-naviga prima di leggere stato

---

## A8 — Decisione adattiva casinò (Slot esaurita)

**Attore**: Raffaele | **Frequenza**: < 20% delle operazioni casinò

### Step-by-step
1. ScreenReader legge saldo €0.00, wagering non raggiunto → task HITL `ADAPTIVE_DECISION`
2. Dashboard: "⚠️ Slot Esaurita | ID-002 | Sisal | Big Bass Bonanza | Saldo: €0.00"
3. Statistiche: Importo iniziale €50, Wagering richiesto €2000, completato €500 (25%), Bonus rimanente €30
4. Opzioni: `[Ricarica €50 e Riprova]` `[Cambia Slot]` `[Chiudi in Perdita]`
5. **Ricarica**: TouchInjector naviga a deposito, pre-compila €50, conferma, autoplay riprende
   **Cambia Slot**: operatore seleziona nuova slot → TouchInjector chiude, naviga, imposta spin minimi, avvia
   **Chiudi**: `COMPLETED` con `profit_book = -€50`, registrato in Netcap

---

## A9 — Gestione quote cambiate (profitto negativo)

**Attore**: Raffaele | **Frequenza**: raro

### Step-by-step
1. CascadeEngine legge quota lay scesa drasticamente (1.95 → 1.50), MultiplierEngine restituisce `is_valid=False`
2. `HITL_BLOCKED`, alert: "⚠️ Quote Cambiate - Profitto Negativo | Quota prevista 1.95 | attuale 1.50 | Perdita assicurata: -€15,00"
3. Opzioni: `[Annulla Bancata]` `[Piazza a Perdita (-€15)]` `[Attendi Miglioramento]`
4. **Annulla**: back scoperto (rischio limitato a €100)
   **Piazza a perdita**: chiude posizione senza gamble
   **Attendi**: monitora quota ogni 30s, se torna profittevole piazza automaticamente, se evento sta per iniziare ri-allerta

---

## A10 — Classificazione movimento atipico

**Attore**: Operatore | **Frequenza**: 1-2 volte/settimana

### Step-by-step
1. MovementImporter legge "Addebito SEPA Apple" - €9.99, nessuna regola matcha → `PENDING`
2. Dashboard: badge giallo "Classificazione richiesta"
3. Operatore apre dropdown: `[Costo]` `[Trasferimento]` `[Deposito Book]` `[Prelievo Book]` `[Ignora]`
4. Seleziona `[Costo]` → RulesEngine applica segno, scrive in Netcap conto "Costi Operativi"

---

## A11 — Input valore oscurato

**Attore**: Raffaele | **Frequenza**: variabile (se CV fallisce emoji animate)

### Step-by-step
1. Segnale con emoji animate (🔡🔡€), CV non riesce a risolverle
2. Task HITL `OBSCURED` → dashboard mostra segnale con placeholder
3. Raffaele legge il segnale originale sul telefono (o via scrcpy), identifica i valori reali
4. Inserisce importi/quote manualmente nei campi appositi
5. Sistema costruisce `Signal` completo e procede con pipeline normale
