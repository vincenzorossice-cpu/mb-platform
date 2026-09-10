# Roadmap di prodotto

```yaml
---
id: ROADMAP
title: "Roadmap di prodotto — Framework di prioritizzazione e fasi (120 giorni)"
status: draft
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 17 (Roadmap di prodotto)
kb_links:
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
  - P5-SPORT-MULTIPLE-CASCATA
  - P4-MOVIMENTAZIONE-ID-PER-ID
related:
  - docs/kpi/README.md (KPI e Definition of Done)
  - docs/risk/register.md (Rischi e mitigazioni)
  - docs/architecture/deployment.md (Infrastruttura e costi)
---
```

> ⚠️ **IMPORTANTE**: La roadmap di prodotto non è ancora definita definitivamente. Questo documento definisce il framework e la roadmap indicativa basata sull'analisi del Libro Mastro, ma sarà rivista dopo:
> 1. Integrazione della knowledge base (~100 file md) — potrebbe rivelare dettagli che cambiano le priorità
> 2. Valutazione del ROI reale di ogni processo automatizzato
> 3. Disponibilità del team (Augusto + dev full-time da integrare)
> 4. Risultati dei test empirici (uiautomator per-book, OCR accuracy, ADB WiFi scalability)

---

## 1. Framework di prioritizzazione

Ogni processo viene valutato su 3 dimensioni:

| Dimensione | Cosa misura | Peso |
|---|---|---|
| **ROI (Return on Investment)** | Ore risparmiate × valore orario / sforzo di sviluppo | 40% |
| **Abilitanza per scalata** | Senza questa automazione, si può scalare a 100? | 35% |
| **Rischio tecnico** | Quanto è complessa l'automazione? (inverso) | 25% |

### Matrice di valutazione

| Processo | ROI | Abilitanza scalata | Rischio tecnico (inv) | Score | Priorità |
|---|---|---|---|---|---|
| **P6 — Riconciliazione** | Alto (24-48h/mese) | Critico (impossibile manuale a 100) | Medio (CV + rules) | 8.5 | 1 |
| **P7 — Contabilità** | Alto (data entry alto volume) | Alto (errori alimentano P6) | Basso (API interna) | 8.0 | 2 |
| **P5 Sport** | Alto (profitto diretto) | Alto (scalare ricavi) | Alto (cascata tempo-reale) | 7.0 | 3 |
| **P4 — Movimenti** | Medio (data entry) | Medio (riduce errori P6) | Medio (PSD2 + CV) | 6.5 | 4 |
| **P5 Casinò** | Medio (varianza) | Medio (volume operazioni) | Alto (decisioni adattive) | 5.5 | 5 |
| **P2 — Apertura book** | Basso (una tantum) | Basso (una tantum) | Medio (form filling + CAPTCHA) | 4.0 | 6 |
| **P1 — Onboarding** | Basso (una tantum) | Basso (una tantum) | Basso (OCR documenti) | 3.5 | 7 |
| **P8 — Credenziali** | Basso (preventivo) | Alto a 100 ID (sicurezza) | Basso (vault) | 4.5 | 8 |

---

## 2. Roadmap indicativa (120 giorni)

### Fase 0: Fondamenta (Settimane 1-3)

| Settimana | Task | Owner | Output |
|---|---|---|---|
| 1 | Integrazione knowledge base (~100 file md) + analisi granularità | Raffaele + Claude Code | KB pronta per RAG |
| 1 | DevTools su Netcap → documentare endpoint API per scrittura | Augusto | API spec Netcap |
| 1 | Test uiautomator dump su 3-5 app di book | Augusto | Tabella book: ✅/❌ uiautomator |
| 1 | Setup scrcpy + ADB su PC di test, 5 telefoni via WiFi | Augusto | Ambiente TEST operativo |
| 1 | Test Betfair web mobile su Chrome Android | Raffaele | Conferma fattibilità cascata via web |
| 2-3 | Think-aloud Raffaele: 8-10h, sessioni 45-90 min | Raffaele + Enzo | Audio registrato |
| 2-3 | Whisper trascrizione + Llama 3 estrazione regole | Augusto | Regole tacite estratte |
| 2-3 | Prototipo ScreenReader (uiautomator + PaddleOCR fallback) | Augusto | Modulo funzionante |
| 2-3 | Prototipo TouchInjector (ADB tap humanizzato via uiautomator XML) | Augusto | Modulo funzionante |

### Fase 1: Prototipo Riconciliazione P6 (Settimane 3-6)

| Settimana | Task | Owner | Output |
|---|---|---|---|
| 3-5 | ScreenReader legge saldi da book → confronto con Netcap → Rules Engine → rettifica | Augusto | Pipeline P6 completa |
| 4-6 | Motore calcolo multipla: implementazione + validazione vs NinjaBet (Playwright, 10 segnali reali) | Augusto + Raffaele | MultiplierEngine validato |
| 5-6 | Validazione P6 su 2-3 identità di test (sistema speculare) | Tutti | P6 funzionante su TEST |
| 5-6 | App API Client: integrazione scrittura rettifiche su Netcap | Augusto | Scrittura Netcap funzionante |

### Fase 2: Data Entry Assistito P7 (Settimane 6-8, parallela)

| Settimana | Task | Owner | Output |
|---|---|---|---|
| 6-7 | ScreenReader legge movimenti da Hype (GoCardless API) e PayPal (CV su schermo) | Augusto | Import movimenti funzionante |
| 7-8 | Pre-compilazione in Netcap con convenzioni di segno corrette | Augusto | Data entry assistito |
| 7-8 | HITL Dashboard: conferme per movimenti e rettifiche | Augusto | Dashboard operativa |

### Fase 3: Assistente Sport P5 (Settimane 8-12)

| Settimana | Task | Owner | Output |
|---|---|---|---|
| 8-9 | Signal Parser: Llama 3 estrazione JSON da segnali Telegram (con CV per emoji animate) | Augusto | Signal Parser funzionante |
| 9-10 | Cascade Engine: state machine + Betfair web mobile via ScreenReader | Augusto | Cascata automatizzata |
| 10-11 | TouchInjector: piazzamento back su book e lay su Betfair | Augusto | Piazzamento automatizzato |
| 11-12 | Validazione P5 sport su 2-3 identità di test con HITL completo | Tutti | P5 sport prototipo |

### Fase 4: Scale-up (Settimane 12-16+)

| Settimana | Task | Owner | Output |
|---|---|---|---|
| 12-13 | OpSec: Correlation Monitor + temporal diversity scheduling | Augusto | OpSec gate attivo |
| 13-14 | Acquisto 20-30 telefoni refurbished + 2 mini-PC + dock + AP | Enzo | Hardware scale |
| 14-15 | Setup 2 host × 20 telefoni, migrazione graduale | Augusto | 40 dispositivi operativi |
| 15-16 | Scale-up 10 → 20 identità | Tutti | 20 identità operative |
| 16+ | Validazione → scale a 50 → 100 | Tutti | 100 identità |

---

## 3. Gate di validazione a 90 giorni

| Criterio | Target | Misurazione |
|---|---|---|
| P6 automatizzato su 10+ identità di test | ✅ | KPI P6 |
| P7 data entry assistito in produzione | ✅ | KPI P7 |
| P5 sport prototipo con HITL funzionante | ✅ | KPI P5 Sport |
| 20+ dispositivi su 2 host operativi | ✅ | Device uptime >95% |
| Import Hype funzionante per 5+ identità | ✅ | GoCardless integration |
| Motore calcolo validato vs NinjaBet | ✅ | ±0.50 EUR per scenario |
| Knowledge base indicizzata in ChromaDB | ✅ | ~100 documenti + regole tacite |

**Se tutti verdi → continuare scale verso 100.**

---

## 4. Roadmap di prodotto — Da definire

La roadmap di prodotto definitiva sarà costruita dopo:
1. **Integrazione della knowledge base** — i ~100 file md potrebbero rivelare procedure più complesse del previsto, dipendenze tra processi non mappate, strategie alternative che cambiano la priorità
2. **Risultati dei test empirici** — se uiautomator è bloccato dal 50% dei book, l'approccio screen-first richiede più tempo del previsto
3. **Capacità del team** — l'inserimento del dev full-time cambia la velocità di esecuzione
4. **Validazione del ROI reale** — dopo 90 giorni di P6 in produzione, si saprà se il risparmio reale è quello stimato

**Questo documento sarà rivisto come v2.1 dopo questi input.**
