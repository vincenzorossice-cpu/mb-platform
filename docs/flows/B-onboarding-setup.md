# Flusso B — Onboarding & Setup

```yaml
---
id: FLOW-B-ONBOARDING
title: "Flusso B — Onboarding & Setup: 7 sotto-flussi (B1-B7)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.3 (versione canonica 9e0af53b)
use_cases: [B1, B2, B3, B4, B5, B6, B7]
kb_links:
  - P1-ONBOARDING-DISPOSITIVI
  - P1-ONBOARDING-DOCUMENTI-SELFIE
  - P1-ONBOARDING-METODI-PAGAMENTO
  - P2-APERTURA-GUIDA-NUOVO-ID
  - P8-CREDENZIALI-IGIENE-ACCOUNT
related:
  - docs/flows/use-case-map.md
---
```

## B1 — Inserimento nuovo collaboratore/identità

**Attore**: Raffaele | **Frequenza**: una tantum per identità (scale 10→100)

### Precondizioni
- Raffaele ha reclutato una nuova persona e raccolto documenti fisici (CI fronte/retro, tessera sanitaria, selfie con documento)

### Postcondizioni
- `Identity` creata con `status=ONBOARDING`
- Dati anagrafici estratti e verificati
- Documenti salvati nel vault crittografato

### Step-by-step
1. Raffaele apre "Onboarding" nella HITL Dashboard, clicca `[Nuova Identità]`
2. Form vuoto: Dati Anagrafici, Documento, Residenza, Strumenti Dedicati, Upload Documenti
3. Raffaele trascina foto documenti → PaddleOCR estrae dati strutturati → campi compilati, confidence bassa evidenziata in giallo
4. Raffaele verifica e corregge (B3): scorre form, confronta con immagini, corregge errori OCR
5. Raffaele inserisce strumenti dedicati (email, telefono, IBAN, carta) — non da OCR
6. Clicca `[Salva e Crea Identità]` → `Identity(status=ONBOARDING)` in PostgreSQL, documenti crittografati AES-256-GCM nel Vault, dati anagrafici in chiaro nel DB

### Validazioni automatiche
- Codice fiscale: checksum algorithm
- Data di nascita: formato valido, età > 18
- CAP: 5 cifre
- IBAN: formato italiano (27 caratteri, IT + checksum)
- Email: formato valido
- Telefono: formato italiano (+39 o 3xx)

### Edge cases
- **OCR non legge documento**: confidence < 0.70 → "Qualità insufficiente, carica nuova foto"
- **Codice fiscale non valido**: errore, salvataggio bloccato
- **Documento scaduto**: warning ma permette procedere (non può operare finché rinnovato)

---

## B2 — Raccolta documenti (OCR assistito)

**Attore**: Operatore | **Frequenza**: una tantum per identità

### Step-by-step
1. Upload 4 file: carta_identita_fronte.jpg, carta_identita_retro.jpg, tessera_sanitaria.jpg, selfie_con_documento.jpg
2. PaddleOCR estrae testo, modello di parsing struttura in campi (nome, cognome, data, numero)
3. UI: immagine zoomabile + dati estratti con confidence per campo (>0.85 verdi, 0.60-0.85 gialli, <0.60 rossi)
4. Passaggio a verifica (B3)

### Edge cases
- **Documento non italiano**: OCR ottimizzato per documenti italiani, estrazione può fallire → compilazione manuale
- **Selfie non processato da OCR**: salvato solo nel vault per verifica book

---

## B3 — Verifica dati estratti

**Attore**: Operatore | **Frequenza**: una tantum per identità

### Step-by-step
1. Dashboard mostra form con dati pre-compilati e indicatori visivi (✅/⚠️/❌)
2. Operatore verifica campo per campo, confronta con immagine documento a fianco
3. Validazione automatica al `[Salva]`: CF checksum, età > 18, CAP 5 cifre, IBAN italiano, email, telefono
4. Se tutto passa → salva identità. Se alcuni falliscono → errori specifici, salvataggio bloccato

---

## B4 — Registrazione conti Hype/PayPal

**Attore**: Operatore | **Frequenza**: una tantum per identità

### Precondizioni
- Identità in stato `ONBOARDING`
- Raffaele ha aperto manualmente conti Hype e PayPal (richiede KYC esterno, non automatizzabile)

### Step-by-step
1. Apre scheda identità → "Status: ONBOARDING — Conti contenitore da registrare"
2. Inserisce IBAN Hype, email Hype, numero carta → `[Verifica Hype]` (sistema verifica checksum IBAN)
3. Inserisce email PayPal → `[Verifica PayPal]`
4. Inserisce password Hype e PayPal → crittografate nel Vault con chiavi `vault://ID006/hype/password`
5. Clicca `[Conferma Conti Aperti]` → `Identity.status` passa a `READY`

### Edge cases
- **Conto Hype non attivo**: consenso PSD2 fallirà → identità resta `READY` ma senza import Hype
- **PayPal blocca per verifica business**: scoperto quando OCR trova schermata di blocco → operatore allertato

---

## B5 — Assegnazione telefono a identità

**Attore**: Augusto/Enzo | **Frequenza**: una tantum per identità

### Precondizioni
- Identità in stato `READY`
- Telefono disponibile (`Device.status=AVAILABLE`)

### Step-by-step
1. Augusto apre "Device Farm" nella dashboard, vede lista dispositivi
2. Seleziona telefono disponibile, clicca `[Assegna a Identità]`, seleziona ID-006
3. Configurazione fisica: installa app bookmaker, inserisce SIM, abilita ADB WiFi, connette WiFi host
4. `[Configurazione Completata]` → `Device.identity_id=ID-006`, `Device.status=ASSIGNED`

### Edge cases
- **ADB non raggiungibile**: "Verifica ADB WiFi e rete corretta"
- **SIM non attiva**: ScreenReader non carica pagine → alert

---

## B6 — Inserimento credenziali nel vault

**Attore**: Operatore | **Frequenza**: una tantum per book per identità

### Step-by-step
1. Apre tab "Credenziali" scheda identità → lista servizi (Hype ✅, PayPal ✅, Betfair ⚠️, Sisal ⚠️)
2. Clicca `[Aggiungi Credenziali Betfair]` → form username + password masked → `[Salva nel Vault]`
3. `CredentialVault.store()` crittografa AES-256-GCM → entità `Credential` nel DB
4. Per ogni book aperto (Flusso C), operatore inserisce credenziali → sistema salva

### Edge cases
- **Password errata**: non verificata al salvataggio → scoperta quando TouchInjector tenta login → Flusso E3

---

## B7 — Attivazione identità

**Attore**: Raffaele | **Frequenza**: una tantum per identità

### Precondizioni
- Identità in stato `READY`
- Conti Hype e PayPal registrati (B4)
- Telefono assegnato (B5)
- Almeno Betfair aperto e credenziali nel vault (B6)
- Almeno 1 book italiano aperto

### Step-by-step
1. Dashboard mostra checklist prerequisiti (tutti ✅)
2. Raffaele clicca `[Attiva Identità]` → sistema verifica prerequisiti → `Identity.status=ACTIVE`
3. Alert Telegram: "✅ Identità ID-006 attivata. Totale identità attive: 11."

### Edge cases
- **Prerequisito mancante**: bottone disabilitato, tooltip mostra prerequisito mancante
- **Consenso PSD2 non attivato**: non bloccante, warning: "Import Hype non disponibile finché non completato"
