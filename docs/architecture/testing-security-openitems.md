# Testing Strategy, Security & Open Items

```yaml
---
id: ARCH-TEST-SEC-OPEN
title: "Testing Strategy, Security/Compliance, Open Items & Dependencies"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 14 (Testing Strategy)
  - Capitolo 15 (Security, Secrets e Compliance)
  - Capitolo 20 (Punti aperti e dipendenze)
kb_links:
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
  - P8-CREDENZIALI-IGIENE-ACCOUNT
---
```

---

## 1. Testing Strategy

### Sistema speculare (ambiente TEST)

Il sistema di produzione (12 telefoni, Panda, identità reali) **non si tocca**. Si costruisce un sistema speculare separato:

| Componente | TEST |
|---|---|
| Hardware | 3-5 telefoni fisici dedicati (modelli diversi) |
| Identità | 2-3 identità di test con conti reali (importi bassi) |
| Netcap | Istanza separata o sandbox |
| Orchestratore | PC di Augusto, stesso codice, config TEST |

### Livelli di test

| Livello | Cosa | Esempi |
|---|---|---|
| **Unitari** | Moduli isolati con dipendenze mockate | MultiplierEngine equal-profit, RulesEngine regole, OpSec correlation |
| **Integrazione** | Interazione tra moduli, DB/Redis reali (in-memory) | Cascade flow completo con mock ScreenReader, recovery da device offline |
| **E2E** | Sistema speculare su telefoni reali | Leggi saldo Goldbet, riconciliazione completa, cascata su Betfair |
| **Validazione uiautomator** | Per-book (21 book) | Quali app bloccano uiautomator → fallback OCR |
| **Validazione motore** | vs NinjaBet via Playwright | 10 segnali reali, tolleranza ±€0.50 |

### Chaos testing

| Test | Cosa simula | Cosa verifica |
|---|---|---|
| Device disconnect | WiFi scollegato a metà cascata | Recovery automatico |
| ADB kill | `adb kill-server` | Circuit breaker + retry |
| Netcap down | Server spento | Coda scritture differite |
| Redis flush | `FLUSHALL` | Recovery cascade da stato reale |
| Layout change | Vecchia versione app | Screen classification → HITL |
| OCR degradation | Screenshot qualità bassa | Confidence → HITL |

### CI/CD

```yaml
on: [push, pull_request]
jobs:
  unit-tests: pytest tests/unit/ --cov
  integration-tests: pytest tests/integration/ (con Redis + PostgreSQL services)
```

### Copertura target

| Modulo | Unit | Integrazione | E2E |
|---|---|---|---|
| MultiplierEngine | >80% | N/A | vs NinjaBet |
| RulesEngine | >80% | N/A | Su saldi reali |
| OpSec | >80% | Mock Redis | N/A |
| CascadeEngine | State machine | Mock screen reader | Telefono reale |
| SignalParser | Parsing JSON | Mock LLM | Segnali reali |

---

## 2. Security, Secrets e Compliance

### Vault centralizzato (sostituisce Telegram)

```python
class CredentialVault:
    # AES-256-GCM a livello applicazione
    async def store(self, identity_id, service, key, value) -> str  # → vault_key
    async def retrieve(self, vault_key) -> str
```

Master key: derivata da password master (PBKDF2, 600k iterazioni), inserita al boot. Non in memoria persistente.

### Cosa va nel vault

| Dato | Vault? |
|---|---|
| Password bookmaker/Betfair/Hype/PayPal | ✅ |
| CVV, numero carta, IBAN | ✅ |
| Documenti d'identità (immagini) | ✅ (file crittografato) |
| Nome, cognome, codice fiscale | ❌ (DB in chiaro, non sono segreti) |

### Crittografia

| Canale | Crittografia |
|---|---|
| Orchestratore ↔ Host Agent | TLS 1.3 |
| Orchestratore ↔ Netcap/GoCardless/Telegram | TLS 1.2+ |
| PostgreSQL/Redis | Crittografia disco (LUKS) |
| Vault segreti | AES-256-GCM applicazione |
| Screenshot HITL | 30 giorni poi cleanup, disco crittografato |

### Audit trail

```sql
CREATE TABLE audit_log (
    id UUID PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    actor VARCHAR(100),      -- "orchestrator" | "operator:augusto" | "system"
    action VARCHAR(100),     -- "write_operation" | "retrieve_credential" | "hitl_resolve"
    entity_type VARCHAR(50), entity_id VARCHAR(100),
    details JSONB, ip_address VARCHAR(45), user_agent VARCHAR(200)
);
-- Append-only: trigger previene UPDATE/DELETE
```

### Privilegi minimi

| Ruolo | Accesso | Non può |
|---|---|---|
| Orchestratore | Vault, DB, Redis, Host API | Modificare audit log |
| Operatore HITL | Dashboard | Modificare operazioni, accedere segreti |
| Augusto (admin) | Tutto | N/A |
| Enzo (ops) | Dashboard, device management | Modificare codice, segreti |
| Dev | Codice, TEST env | Accesso a PROD |

### Rotazione credenziali

| Credenziale | Frequenza |
|---|---|
| Master key vault | 6 mesi |
| API key Netcap | 90 giorni |
| GoCardless API key | 90 giorni |
| Consenso PSD2 AIS | 90 giorni (SCA collaboratore) |
| Password bookmaker | Su richiesta |
| Certificati TLS | 12 mesi (Let's Encrypt auto) |

### GDPR

| Aspetto | Azione |
|---|---|
| Documenti su Telegram | ❌ Non compliant → migrazione a vault |
| Dati anagrafici nel DB | Aggiungere informativa privacy |
| Log automazione | Retention 90 giorni |
| Diritto all'oblio | Procedura cancellazione identità |
| DPA con GoCardless | Da firmare |

---

## 3. Punti aperti e dipendenze

### Prima della Fase 0

| # | Punto | Owner | Scadenza |
|---|---|---|---|
| 1 | Credenziali Netcap | Augusto | 48h |
| 2 | Integrazione KB (~100 file md) | Raffaele + Claude Code | 1 settimana |
| 3 | Dev full-time | Raffaele + Enzo | 1 settimana |

### Durante la Fase 0

| # | Punto | Owner | Scadenza |
|---|---|---|---|
| 4 | Endpoint API Netcap (DevTools) | Augusto | 2 giorni |
| 5 | Test uiautomator per-book | Augusto | 3 giorni |
| 6 | GoCardless pricing per 100 conti | Augusto | 3 giorni |
| 7 | Think-aloud 8-10h | Raffaele + Enzo | 2-3 settimane |
| 8 | Betfair web mobile test | Raffaele | 1 giorno |

### Durante la Fase 1

| # | Punto | Owner | Scadenza |
|---|---|---|---|
| 9 | Validazione motore vs NinjaBet (10 segnali) | Augusto + Raffaele | 4-6 settimane |
| 10 | OCR accuracy reale (10 screenshot) | Augusto | 2 settimane |
| 11 | Path navigazione per-book (think-aloud) | Raffaele | 2-3 settimane |

### Prima della Fase 3

| # | Punto | Owner | Scadenza |
|---|---|---|---|
| 12 | 2-3 identità test sport | Raffaele | Prima settimana 8 |
| 13 | 2-3 account Betfair test | Raffaele | Prima settimana 8 |

### Prima della Fase 4 (Scale)

| # | Punto | Owner | Scadenza |
|---|---|---|---|
| 14 | Veicolo societario per 100 identità | Raffaele | Prima settimana 12 |
| 15 | Vault credenziali (sostituisce Telegram) | Augusto | Prima settimana 12 |
| 16 | Infrastruttura fisica (rack, clima, alimentazione) | Enzo | Prima settimana 13 |
| 17 | 100 SIM dati a prezzo ridotto | Enzo | Prima settimana 13 |

### Conferme ricevute (Appendice A)

Screen-first (Raffaele), API solo Netcap (Raffaele), Betfair per-account (Raffaele), Modular Monolith (Raffaele), Fee Betfair 4,5% (Raffaele), Latenza 10-30s OK (Raffaele), Sistema speculare (Raffaele), Capital disponibile (Raffaele), redroid escluso (Audit), PayPal API esclusa (Audit), GoCardless Banca Sella (Audit), uiautomator senza root (Audit), ADB WiFi ~20/host (Audit), Betfair web mobile OK (Audit), NinjaBet no API €19,98/mese (Audit), Equal-profit (Audit), Whisper WER 5-7% (Audit), Llama 3 8B 6-10GB (Audit).
