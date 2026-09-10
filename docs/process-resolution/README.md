# Process Resolution — As-Is / To-Be / HITL per processo

```yaml
---
id: PROC-RESOLUTION
title: "Risoluzione dei processi — As-Is / To-Be / HITL residuo"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 8 (per esteso)
kb_links:
  - P1-ONBOARDING-DISPOSITIVI
  - P2-APERTURA-SCALETTA-BOOK
  - P3-ANTILIMIT-PRINCIPI
  - P4-BANKROLL-GESTIONE
  - P4-MOVIMENTAZIONE-ID-PER-ID
  - P5-SPORT-MULTIPLE-CASCATA
  - P5-CASINO-SLOT-FONDAMENTI
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
  - P8-CREDENZIALI-IGIENE-ACCOUNT
---
```

## Modello di risoluzione

Per ogni processo del Libro Mastro (KB P1-P8), definiamo:
1. **As-Is**: lo standard operativo reale attuale, con i suoi colli di bottiglia
2. **To-Be**: come l'architettura screen-first lo automatizza
3. **HITL residuo**: quali decisioni o azioni richiedono ancora intervento umano

> **Nota sulla roadmap**: La priorità di attacco di questi processi (P6 → P7 → P5 Sport, ecc.) è indicativa e basata sull'analisi del ritorno atteso. La roadmap di prodotto definitiva verrà costruita insieme dopo l'integrazione della knowledge base e i primi test empirici.

> **Nota su P3 (Profilazione/Anti-limit)**: P3 non è un processo separato nel Libro Mastro con un proprio flusso As-Is/To-Be, ma una **preoccupazione cross-cutting** trasversale a tutti i processi. La profilazione anti-limit, la prevenzione del gubbing e la diversificazione comportamentale sono gestite dal modulo OpSec (vedi `docs/modules/core-agents.md#4-opsec`). Le guide KB P3-* (profilazione book, anti-limit, gubbing prevention, provenienza IP) sono referenziate nei KB links di OpSec e applicate trasversalmente prima di ogni operazione.

---

## P6 — Riconciliazione saldi mensile

### As-Is
Oggi la riconciliazione è interamente manuale ed è l'attività più pesante dell'intero ciclo, richiedendo 24-48 ore di lavoro al mese. Si accede a ogni book di ogni identità tramite i telefoni Android collegati al PC con Panda, si legge il saldo reale, lo si confronta con il saldo atteso dell'app (pagina Saldi Bookmaker) e, se c'è differenza, si registra un'operazione di rettifica. Si riconciliano solo i conti in stato "Aperto" o "Limitato".

*Fonte KB: P6-RICONCILIAZIONE-PROCEDURA*

### To-Be
1. Il modulo `ScreenReader` accede a ogni book su ogni telefono (uiautomator o OCR fallback)
2. Legge il saldo reale dallo schermo
3. Il modulo `AppApiClient` legge il saldo atteso da Netcap
4. Il modulo `RulesEngine` applica le regole di riconciliazione:
   - Giocate aperte: sommate al saldo
   - Real bonus: contato come saldo
   - Bonus con wagering: non contato, segnalato in nota
   - Betfair: differenza in colonna BANCO non BOOK
5. Se la differenza è > 0 (e supera la tolleranza di ±1 EUR): `AppApiClient` scrive la rettifica in Netcap

### HITL residuo
- Conferma delle rettifiche con importo elevato (es. > €50)
- Gestione di casi non coperti dalle regole (il `RAGSystem` suggerisce un'azione, l'operatore conferma)
- Risoluzione di letture OCR ambigue (quando la confidence è < 0.85)

---

## P7 — Contabilità e data entry

### As-Is
Il processo prevede la registrazione manuale di ogni operazione e movimento nell'app gestionale, con doppia convenzione di segno (book vs Hype/PayPal). Non esiste alcun collegamento automatico con i conti reali (Hype, PayPal, book).

*Fonte KB: P7-APP-CONVENZIONI-SEGNO, P7-APP-CALCOLO-SALDI-PROFITTI*

### To-Be
1. **Operazioni (P5)**: registrazione automatica via `AppApiClient` dopo ogni operazione conclusa
2. **Movimenti**: pre-compilazione assistita
   - `MovementImporter` legge i movimenti da Hype via API GoCardless (PSD2)
   - `MovementImporter` legge i movimenti da PayPal via `ScreenReader` (OCR)
   - `AppApiClient` pre-compila i dati in Netcap applicando automaticamente le convenzioni di segno corrette
3. L'operatore verifica e conferma

### HITL residuo
- Conferma dei movimenti prima del salvataggio definitivo
- Classificazione di movimenti atipici non riconosciuti
- Rinnovo del consenso PSD2 per Hype (ogni 90 giorni, richiede SCA del collaboratore)

---

## P5 SPORT — Esecuzione bonus sport

### As-Is
Il processo prevede la lettura del segnale Telegram, la verifica manuale dei T&C, la compilazione di NinjaBet MultiOdds per calcolare puntate e responsabilità, il piazzamento del back sul book e la gestione manuale della cascata su Betfair evento per evento.

*Fonte KB: P5-SEGNALE-LETTURA-INTERPRETAZIONE, P5-SPORT-MULTIPLE-CASCATA, P5-STRUMENTI-MULTIPLICATORE-MULTIODDS*

### To-Be
1. `SignalParser` (Llama 3) parsea il segnale Telegram in JSON strutturato
2. `RAGSystem` verifica i T&C del book confrontandoli con la knowledge base
3. `MultiplierEngine` calcola puntata, responsabilità e scenari (strategia equal-profit, fee Betfair 4,5%)
4. `TouchInjector` piazza il back sul book
5. `TouchInjector` piazza la bancata su Betfair sul telefono dell'identità
6. `CascadeEngine` gestisce la cascata monitorando lo stato via `ScreenReader` e avanzando automaticamente

### HITL residuo
- Conferma dei parametri prima del piazzamento del back
- Gestione errori (es. quote cambiate significativamente, slot bloccata, promo non caricata)
- Gestione dei bonus esclusivi (troppo specifici e legati alla profilazione per un'automazione standardizzata)

---

## P5 CASINÒ — Esecuzione bonus casinò

### As-Is
Il processo prevede l'adesione alla promo e il gioco alle slot con strategie specifiche (es. spin alti per creare un margine, raggiungimento del target, passaggio a spin minimi per completare il wagering). Ha una forte componente di varianza.

*Fonte KB: P5-CASINO-SLOT-FONDAMENTI, P5-CASINO-FONDAMENTI-RTP-WAGERING, P5-CASINO-ROLLOVER-CONTRIBUZIONE*

### To-Be
1. `SignalParser` parsea il segnale (slot consigliata, spin, target, tipo bonus)
2. `TouchInjector` naviga alla slot e avvia l'autoplay (funzione nativa)
3. `ScreenReader` monitora il saldo verso il target
4. Al raggiungimento del target, `TouchInjector` modifica l'importo dello spin per il wagering
5. `ScreenReader` monitora il contatore wagering

### HITL residuo
- **Decisioni adattive in tempo reale**: se la slot si esaurisce o la varianza è fortemente negativa, il sistema deve decidere se continuare, cambiare slot o fermarsi. Queste decisioni richiedono il `RAGSystem` e l'intervento umano di conferma.

---

## P4 — Movimentazione fondi

### As-Is
Depositi e prelievi manuali tra conti Hype/PayPal e book, con registrazione manuale nell'app. La scelta dell'importo non segue regole rigide ma si basa sul mantenimento dei conti "carichi".

*Fonte KB: P4-BANKROLL-GESTIONE, P4-MOVIMENTAZIONE-ID-PER-ID*

### To-Be
1. Lettura automatica dei movimenti via `MovementImporter` (Hype via API, PayPal via schermo)
2. Pre-compilazione in Netcap con convenzioni di segno corrette via `AppApiClient`
3. L'esecuzione materiale del deposito/prelievo sui book rimane un'interazione del `TouchInjector` quando serve per un'operazione specifica

### HITL residuo
- Conferma dei movimenti
- Approvazione di trasferimenti di capitale tra identità o verso il conto centrale

---

## P2 — Apertura conti bookmaker

### As-Is
Apertura manuale seguendo la guida "Apertura nuovo ID" v4, con i vincoli di famiglia (tempi minimi tra aperture dello stesso gruppo) gestiti a memoria.

*Fonte KB: P2-APERTURA-SCALETTA-BOOK, P2-APERTURA-GUIDA-NUOVO-ID, P2-APERTURA-ELENCO-BOOK*

### To-Be
1. Alert automatico sui vincoli di famiglia: il sistema calcola quando è possibile aprire un book dello stesso gruppo in base alle date di apertura già registrate in Netcap
2. Pre-compilazione dei form: i dati anagrafici dell'identità vengono estratti dal database e inseriti nei form dei book via `TouchInjector`

### HITL residuo
- Gestione delle verifiche live (book che richiedono la presenza fisica del collaboratore)
- Gestione di Bet365 (da trattare con cautela)
- Risoluzione di CAPTCHA complessi durante la registrazione

---

## P1 — Onboarding nuovo collaboratore

### As-Is
Raccolta manuale dei documenti, trascrizione manuale su Telegram con un template fisso, creazione di strumenti dedicati (email, telefono, Hype, PayPal).

*Fonte KB: P1-ONBOARDING-DISPOSITIVI, P1-ONBOARDING-DOCUMENTI-SELFIE, P1-ONBOARDING-METODI-PAGAMENTO*

### To-Be
1. OCR (PaddleOCR) estrae i dati anagrafici dai documenti (carta d'identità, tessera sanitaria)
2. Pre-compilazione del template di registrazione nel sistema
3. Salvataggio sicuro nel vault (sostituendo l'archivio Telegram)

### HITL residuo
- Verifica dei dati estratti dall'OCR
- Apertura materiale dei conti Hype/PayPal (richiede KYC e interazione del collaboratore reale)
- Reclutamento del collaboratore (inherently umano)

---

## P8 — Gestione credenziali e scadenze

### As-Is
Credenziali conservate su Telegram in via provvisoria. Scadenze bonus gestite poco nell'app.

*Fonte KB: P8-CREDENZIALI-IGIENE-ACCOUNT, P8-CREDENZIALI-BONUS-COMPLEANNO*

### To-Be
1. Vault centralizzato per le credenziali (sostituisce Telegram)
2. Alert automatici sulle scadenze bonus (adesione e completamento)
3. Dashboard con widget "Prossime scadenze"

### HITL residuo
- Gestione di accessi bloccati o reset password
- Decisione strategica su quali bonus prioritizzare in caso di scadenze ravvicinate
