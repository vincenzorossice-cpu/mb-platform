# Use Case Map — 48 casi di interazione utente

```yaml
---
id: FLOWS-USE-CASE-MAP
title: "Mappatura di TUTTI i casi di interazione utente (48 casi, 8 categorie)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.1 (versione canonica f1e09650)
related:
  - docs/flows/A-daily-operations.md
  - docs/flows/B-onboarding-setup.md
  - docs/flows/CDEF-bookmaker-devices-credentials-kb.md
  - docs/flows/GH-monitoring-reconciliation.md
---
```

## Categoria A — Daily Operations (HITL operative quotidiane)

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| A1 | Conferma rettifica saldo | Differenza > €1 EUR su riconciliazione P6 | Operatore | 2-5 volte/mese per identità |
| A2 | Conferma parametri operazione sport | Prima di piazzare back sul book | Raffaele | Ogni operazione sport |
| A3 | Conferma movimenti importati | Movimento letto da Hype/PayPal, pre-compilato in Netcap | Operatore | 5-10 volte/giorno |
| A4 | Risoluzione OCR low confidence | ScreenReader confidence < 0.85 | Operatore | Variabile (dipende da book) |
| A5 | Risoluzione CAPTCHA | Book presenta CAPTCHA durante navigazione | Operatore (o 2Captcha auto) | Raro |
| A6 | Gestione discrepanza T&C | Segnale dice X, knowledge base dice Y | Raffaele | < 15% dei segnali |
| A7 | Gestione errore cascata | Device offline, bet not confirmed, state inconsistent | Raffaele o Augusto | Raro ma critico |
| A8 | Decisione adattiva casinò | Slot si esaurisce, varianza negativa | Raffaele | < 20% delle operazioni casinò |
| A9 | Gestione quote cambiate (profitto negativo) | Ricalcolo cascade produce profitto negativo | Raffaele | Raro |
| A10 | Classificazione movimento atipico | Movimento non riconosciuto (es. rimborso Google Play) | Operatore | 1-2 volte/settimana |
| A11 | Input valore oscurato | Segnale Telegram con emoji animate non risolvibili via CV | Raffaele | Variabile |

## Categoria B — Onboarding & Setup (non quotidiano, ma critico)

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| B1 | Inserimento nuovo collaboratore/identità | Nuova persona reclutata | Raffaele | Una tantum per identità |
| B2 | Raccolta documenti (OCR assistito) | Parte del onboarding | Operatore | Una tantum per identità |
| B3 | Verifica dati estratti da documenti | Dopo OCR di carta d'identità | Operatore | Una tantum per identità |
| B4 | Registrazione apertura conti Hype/PayPal | Dopo creazione conti | Operatore | Una tantum per identità |
| B5 | Assegnazione telefono a identità | Telefono fisico pronto | Augusto/Enzo | Una tantum per identità |
| B6 | Inserimento credenziali nel vault | Dopo apertura book/conti | Operatore | Una tantum per book per identità |
| B7 | Attivazione identità | Onboarding completo | Raffaele | Una tantum per identità |

## Categoria C — Apertura conti bookmaker (P2)

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| C1 | Avvio apertura nuovo book | Identità pronta per nuovo book | Raffaele | 20-30 volte per identità |
| C2 | Verifica vincoli di famiglia | Sistema rileva possibile violazione | Operatore | Automatico + alert |
| C3 | Gestione verifica live | Book richiede presenza collaboratore | Raffaele + collaboratore | 5 book specifici |
| C4 | Gestione CAPTCHA registrazione | CAPTCHA durante form di registrazione | Operatore | Variabile |
| C5 | Conferma apertura book registrata | Book aperto con successo | Operatore | Ogni apertura |
| C6 | Gestione Bet365 (cautela) | Apertura Bet365 | Raffaele | Una tantum per identità |

## Categoria D — Gestione dispositivi

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| D1 | Aggiunta nuovo telefono al sistema | Nuovo telefono fisico acquistato | Augusto/Enzo | Scale-up |
| D2 | Assegnazione telefono a identità | Telefono configurato e pronto | Augusto | Una tantum per identità |
| D3 | Rimozione telefono (guasto) | Telefono irrecuperabile | Augusto | Raro |
| D4 | Manutenzione telefono (ricarica, riparazione) | Batteria < 15% o guasto fisico | Enzo | Rotazione |
| D5 | Sostituzione telefono | Telefono sostituito, identità da migrare | Augusto | Raro |

## Categoria E — Gestione credenziali e consensi (P8)

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| E1 | Avvio flusso consenso PSD2 AIS per Hype | Nuova identità o consenso scaduto | Operatore + collaboratore | Ogni 90 giorni per identità |
| E2 | Rinnovo consenso PSD2 scaduto | Alert 7 giorni prima scadenza | Operatore + collaboratore | Ogni 90 giorni per identità |
| E3 | Reset password book | Book richiede reset o sospetto compromissione | Operatore | Raro |
| E4 | Gestione account bloccato | Book blocca account | Raffaele | Raro ma critico |
| E5 | Rotazione API key Netcap | Ogni 90 giorni | Augusto | Ogni 90 giorni |
| E6 | Rotazione master key vault | Ogni 6 mesi | Augusto | Ogni 6 mesi |

## Categoria F — Gestione knowledge base

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| F1 | Aggiornamento procedura book | Book cambia layout o T&C | Raffaele + Augusto | Variabile |
| F2 | Aggiunta nuova regola tacita | Think-aloud o scoperta operativa | Raffaele → Augusto | Continuo |
| F3 | Validazione regola (periferia → active) | Regola proposta nella periferia | Augusto (sessione di validazione) | Settimanale |
| F4 | Deprecazione regola | Regola non più valida | Augusto | Raro |
| F5 | Indicizzazione nuovo documento in ChromaDB | Nuovo file md nella KB | Augusto (automatico) | Ogni aggiornamento KB |

## Categoria G — Monitoring e ops

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| G1 | Check giornaliero stato sistema | Routine mattutina | Enzo | Giornaliero |
| G2 | Risposta ad alert critico | Alert SMS/Telegram critical | Augusto o Raffaele | Raro ma immediato |
| G3 | Gestione Netcap down | Circuit breaker OPEN | Augusto + Enzo | Raro |
| G4 | Gestione Redis failure | Stato cascade perso | Augusto | Raro |
| G5 | Post-mortem operazione fallita | Operazione in FAILED o DLQ | Augusto | Settimanale |
| G6 | Review profitti giornalieri/settimanali | Routine | Raffaele | Giornaliero/settimanale |

## Categoria H — Riconciliazione manuale (fallback)

| # | Caso | Trigger | Chi agisce | Frequenza |
|---|---|---|---|---|
| H1 | Rettifica manuale (sistema non riesce) | ScreenReader fallisce su book specifico | Raffaele | Raro (decrescente con maturità) |
| H2 | Verifica post-riconciliazione | Controllo qualità random | Raffaele | Settimanale |

---

## Sintesi: 48 casi di interazione utente mappati

| Categoria | Casi | Frequenza tipica |
|---|---|---|
| A — Daily Operations | 11 | Più volte al giorno |
| B — Onboarding | 7 | Una tantum per identità |
| C — Apertura book | 6 | 20-30 per identità |
| D — Dispositivi | 5 | Scale-up e manutenzione |
| E — Credenziali/consensi | 6 | Ogni 90 giorni |
| F — Knowledge base | 5 | Continuo |
| G — Monitoring/ops | 6 | Giornaliero + on-demand |
| H — Riconciliazione manuale | 2 | Fallback raro |
| **Totale** | **48** | |
