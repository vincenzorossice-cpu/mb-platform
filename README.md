# MB Platform — Documentation Index

```yaml
---
id: README
title: "MB Platform — Documentazione di prodotto (Docs-as-Code, agentic-first)"
status: active
created: 2026-07-23
updated: 2026-07-23
version: "v2.0"
---
```

## Benvenuto

Documentazione di prodotto per la piattaforma di automazione matched betting **screen-first** e **agentico-first**. Tutta la documentazione è in markdown, versionata in git, con frontmatter YAML per tracciabilità.

## Struttura della documentazione (24 documenti)

### Architecture (5)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/architecture/overview.md` | ARCH-OVERVIEW | Visione, 10 moduli, stack, mappa processi→moduli, KB tracciabilità |
| `docs/architecture/data-model.md` | ARCH-DATA-MODEL | 11 entità, relazioni, dataclass, stati, idempotency |
| `docs/architecture/technical.md` | ARCH-TECHNICAL | 3 layer fisici, stack software, struttura codice, Host Agent, deploy, backup |
| `docs/architecture/deployment.md` | ARCH-DEPLOYMENT | Hardware 100 telefoni, costi ($9K-18K one-time, $1.1K-2.3K/mese), GPU, consumo |
| `docs/architecture/testing-security-openitems.md` | ARCH-TEST-SEC-OPEN | Testing strategy, security/vault/GDPR, 17 punti aperti, conferme |

### ADR (2)
| Documento | Note ID | Decisione |
|---|---|---|
| `docs/adr/0001-agentic-architecture.md` | ADR-0001 | Sincrono, Result types, agentico, rationale, alternative |
| `docs/adr/0002-screen-first.md` | ADR-0002 | Screen-first, zero API, 3 pilastri, conferme ricevute |

### Contracts (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/contracts/README.md` | CONTRACTS-ALL | Schema input/output + error codes per 10 moduli (documento consolidato con sezioni per modulo) |

### Modules (4)
| Documento | Note ID | Moduli |
|---|---|---|
| `docs/modules/signal-parser/README.md` | MOD-SIGNAL-PARSER | Agent README: responsabilità, confini, pipeline, HITL, metriche |
| `docs/modules/signal-parser/design.md` | MOD-SIGNAL-PARSER-DESIGN | 3 varianti segnali, emoji CV, regex, LLM, postproc, T&C, edge cases |
| `docs/modules/error-handling-system.md` | SYS-ERROR-HANDLING | 3 livelli failure, checkpoint, retry, circuit breaker, watchdog, DLQ, matrice decisione |
| `docs/modules/core-agents.md` | MOD-CORE-AGENTS | MultiplierEngine, CascadeEngine, RAGSystem, OpSec (4 Agent README) |

### Flows (5)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/flows/use-case-map.md` | FLOWS-USE-CASE-MAP | 48 casi in 8 categorie A-H con sintesi |
| `docs/flows/A-daily-operations.md` | FLOW-A-DAILY-OPS | 11 flussi HITL quotidiani (A1-A11) |
| `docs/flows/B-onboarding-setup.md` | FLOW-B-ONBOARDING | 7 sotto-flussi onboarding (B1-B7) |
| `docs/flows/CDEF-bookmaker-devices-credentials-kb.md` | FLOWS-CDEF | Flussi C/D/E/F (book, dispositivi, credenziali, KB) |
| `docs/flows/GH-monitoring-reconciliation.md` | FLOWS-GH-MONITORING | Flussi G (monitoring), H (riconciliazione manuale) |

### HITL (2)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/hitl/dashboard-design.md` | HITL-DASHBOARD | Mission Control v2 Audit, 3 colonne, componenti, mockup |
| `docs/hitl/design-system.md` | HITL-DESIGN-SYSTEM | Design System, 4 Epics, 10 User Stories, requisiti non funzionali |

### Process Resolution (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/process-resolution/README.md` | PROC-RESOLUTION | As-Is/To-Be/HITL per P1, P2, P3 (nota), P4, P5 Sport, P5 Casinò, P6, P7, P8 |

### KPI (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/kpi/README.md` | KPI-DOD | 9 KPI sistema, KPI per processo, DoD per Fasi 0-4 |

### Risk (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/risk/register.md` | RISK-REGISTER | 8 rischi tecnici, 6 operativi, 5 business, risk matrix, top 5 |

### Roadmap (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `docs/roadmap/README.md` | ROADMAP | Framework prioritizzazione, 4 fasi 120 giorni, gate 90 giorni |

### Ops (1)
| Documento | Note ID | Contenuto |
|---|---|---|
| `ops/runbooks/incident-response.md` | OPS-OBSERVABILITY | Logging, metriche, tracing, 4 runbook (device, OCR, cascade, Netcap) |

## Statistiche

- **Documenti prodotti**: 24
- **Capitoli sorgente riscritti**: 22 (da 40 file nella KB MB PRD)
- **Guide KB referenziate**: 140 (KB "MB Brain", processi P1-P8)
- **Moduli documentati**: 10 (con Agent README, contratti, error codes)
- **Flussi operativi**: 48 casi in 8 categorie
- **ADR**: 2
- **Versione**: v2.0 (engineering-ready)

## Fonti

| Fonte | ID |
|---|---|
| Libro Mastro dei Processi | [1] |
| Appunti sessione 15 luglio 2026 | [2] |
| Audit esterno Round 1 (Grok + Fable 5) | API, anti-bot, hardware, PSD2, NinjaBet |
| Audit esterno Round 2 (Grok + Fable 5) | Screen-first: uiautomator, OCR, ADB WiFi |
| Feedback CTO Senior | Data model, contratti, error handling, testing, security |
| Knowledge base "MB Brain" (140 guide) | P1-P8, processi operativi |
