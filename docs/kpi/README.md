# KPI e Definition of Done

```yaml
---
id: KPI-DOD
title: "KPI di sistema, KPI per processo, Definition of Done per fase"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 18 (KPI e Definition of Done)
related:
  - docs/roadmap/README.md (Roadmap)
  - docs/risk/register.md (Rischi)
---
```

## 1. KPI di sistema (generali)

Questi KPI misurano la salute del sistema nel suo complesso, indipendentemente dal processo.

| KPI | Definizione | Target | Misurazione |
|---|---|---|---|
| **Uptime orchestratore** | % tempo in cui l'orchestratore è raggiungibile | > 99% | `/health` endpoint, Prometheus |
| **Uptime dispositivi** | % dispositivi online su totali | > 95% | `devices_online / devices_total` gauge |
| **Latenza P95 ScreenReader** | Tempo dal commando al risultato | < 5s | Histogram `screen_read_latency_seconds` |
| **Latenza P95 touch injection** | Tempo dal comando ADB al confirmation | < 3s | Histogram `tap_latency_seconds` |
| **HITL pending critici** | Richieste HITL critical non risolte | 0 (sempre) | Gauge `hitl_pending{severity="critical"}` |
| **Tempo medio HITL resolution** | Tempo medio per risolvere un HITL | < 30 min (warning), < 15 min (critical) | Histogram `hitl_resolution_time_seconds` |
| **Netcap write success rate** | % scritture Netcap riuscite (inclusi retry) | > 99% | Counter `netcap_writes` |
| **Errori di idempotenza** | Duplicati con dati divergenti | 0 | Conteggio `DUPLICATE` con dati divergenti |
| **OpSec blocks** | % operazioni bloccate da OpSec | < 1% | Counter `opsec_blocks_total` |

---

## 2. KPI per processo

### 2.1 — KPI P6 (Riconciliazione)

| KPI | Target | Misurazione |
|---|---|---|
| Tempo riconciliazione per identità (10 book) | < 15 min (vs 2.4-4.8h manuale) | `operation_traces.total_duration_ms` |
| Success rate ScreenReader (no HITL) | > 85% | `screen_reads_total{result="success"} / total` |
| Accuracy uiautomator (vs manuale) | > 95% | Confronto su campione manuale |
| OCR accuracy (quando uiautomator bloccato) | > 80% | Confronto su campione manuale |
| HITL rate per riconciliazione | < 10% dei conti | `hitl_pending / total_accounts_reconciled` |
| Errori di rettifica (rettifiche errate) | 0 | Confronto post-riconciliazione manuale di Raffaele |
| Riconciliazione completata entro mese | 100% dei conti Aperto/Limitato | Query DB |

### 2.2 — KPI P7 (Contabilità)

| KPI | Target | Misurazione |
|---|---|---|
| Movimenti importati automaticamente | > 80% dei movimenti totali | `movements_imported{source="auto"} / total` |
| Errori di segno (convenzione inversa) | 0 | Confronto post-verifica |
| Tempo data entry per movimento | < 30s (vs 2-3 min manuale) | Histogram |
| HITL rate per movimenti | < 15% | `hitl_pending / total_movements` |
| Scadenze consenso PSD2 rispettate | 100% (nessun consenso scaduto senza alert) | Query DB |

### 2.3 — KPI P5 Sport

| KPI | Target | Misurazione |
|---|---|---|
| Success rate piazzamento back | > 90% | `bet_placement{type="back",result="success"}` |
| Success rate piazzamento lay | > 85% | `bet_placement{type="lay",result="success"}` |
| Tempo medio cascata (3 eventi) | < 4 ore (event timing dependent) | `operation_traces` |
| Recovery rate (cascade interrotte recuperate) | > 80% | `cascade_recovery{result="success"} / total` |
| HITL rate per operazione sport | < 15% | `hitl_pending / total_operations` |
| Profitto per operazione vs atteso | ±5% (varianza quote) | `operation.profit_book + operation.profit_banco` vs `multiplier_engine.profit_per_scenario` |
| T&C discrepancy rate | < 15% | `signals_tc_discrepancy / signals_parsed` |

### 2.4 — KPI P5 Casinò

| KPI | Target | Misurazione |
|---|---|---|
| Success rate avvio autoplay | > 90% | `casino_autoplay{result="success"}` |
| Wagering completato | > 90% | `casino_wagering_completed / started` |
| HITL rate per decisioni adattive | < 20% | `hitl_pending{reason="adaptive"} / total_casino_ops` |
| Profitto medio per operazione | Variabile (dipende da varianza) | Confronto con expected value del segnale |

### 2.5 — KPI P4 (Movimenti)

| KPI | Target | Misurazione |
|---|---|---|
| Movimenti letti automaticamente | > 80% | `movements_imported{source="auto"}` |
| Accuracy import Hype (GoCardless) | > 95% | Confronto manuale campione |
| Accuracy import PayPal (OCR) | > 80% | Confronto manuale campione |
| Deduplicazione efficace | 0 duplicati scritti in Netcap | Conteggio `DUPLICATE` |

---

## 3. Definition of Done per fase

### 3.1 — Fase 0 (Fondamenta) — DoD

| Criterio | Verificato da |
|---|---|
| Knowledge base (~100 file md) integrata e indicizzata in ChromaDB | Augusto |
| Endpoint API Netcap documentati | Augusto |
| Tabella uiautomator per-book completata (almeno 5 book) | Augusto |
| Ambiente TEST operativo (5 telefoni + 1 host + orchestratore) | Augusto |
| Think-aloud completato (8-10h) | Raffaele + Enzo |
| Regole tacite estratte e classificate | Augusto |
| Prototipo ScreenReader funzionante (uiautomator + OCR fallback) | Augusto |
| Prototipo TouchInjector funzionante (tap via uiautomator XML) | Augusto |

### 3.2 — Fase 1 (P6 Riconciliazione) — DoD

| Criterio | Verificato da |
|---|---|
| P6 automatizzato su 2-3 identità di test | Augusto + Raffaele |
| KPI P6: tempo riconciliazione < 15 min per identità | Augusto |
| KPI P6: success rate ScreenReader > 85% | Augusto |
| KPI P6: 0 errori di rettifica (vs verifica manuale Raffaele) | Raffaele |
| Scrittura rettifiche su Netcap funzionante | Augusto |
| HITL Dashboard operativa per conferme | Augusto |
| MultiplierEngine validato vs NinjaBet (±0.50 EUR) | Augusto + Raffaele |

### 3.3 — Fase 2 (P7 Data Entry) — DoD

| Criterio | Verificato da |
|---|---|
| Import Hype via GoCardless funzionante per 2+ identità | Augusto |
| Import PayPal via OCR funzionante | Augusto |
| Pre-compilazione movimenti in Netcap con segni corretti | Augusto |
| KPI P7: 0 errori di segno | Raffaele |
| HITL per conferma movimenti operativa | Augusto |

### 3.4 — Fase 3 (P5 Sport) — DoD

| Criterio | Verificato da |
|---|---|
| Signal Parser funzionante su segnali reali (success rate > 90%) | Augusto |
| Cascade Engine completa almeno 3 cascate su identità di test | Augusto + Raffaele |
| KPI P5: success rate back > 90% | Augusto |
| KPI P5: success rate lay > 85% | Augusto |
| HITL per conferma parametri operativa | Raffaele |
| Recovery cascade testato (device offline → recovery) | Augusto |

### 3.5 — Fase 4 (Scale-up) — DoD

| Criterio | Verificato da |
|---|---|
| OpSec gate attivo (correlation monitor + temporal scheduling) | Augusto |
| 20+ dispositivi su 2 host operativi (uptime > 95%) | Augusto + Enzo |
| Scale 10 → 20 identità senza incidenti | Tutti |
| KPI sistema: uptime orchestratore > 99% | Augusto |
| KPI sistema: HITL critical sempre 0 | Augusto |
