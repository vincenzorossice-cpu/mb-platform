# Observability e Operabilità

```yaml
---
id: OPS-OBSERVABILITY
title: "Observability — Logging, Metriche, Tracing, Alerting, Runbook"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 7 (Observability e operabilità)
related:
  - docs/modules/error-handling-system.md
  - docs/kpi/README.md
---
```

## 1. Quattro pilastri

| Pilastro | Tecnologia | Scope |
|---|---|---|
| Logging strutturato | Python structlog → JSON | Tutti i moduli |
| Metriche | Prometheus + Grafana | Dispositivi, moduli, operazioni |
| Tracing | OpenTelemetry | Operazioni critiche (P5 sport, P6 riconciliazione) |
| Alerting | WebSocket + Telegram + SMS | HITL, state changes, critical |

## 2. Logging strutturato

Ogni log entry è JSON con: `timestamp`, `level`, `logger`, `event`, `operation_id`, `identity_id`, `device_id`, `duration_ms`, `result`, `message`.

| Level | Quando | Esempi |
|---|---|---|
| DEBUG | Solo DEV/TEST | XML parseato, coordinate tap, OCR raw |
| INFO | Eventi normali | Segnale ricevuto, operazione creata, saldo letto |
| WARNING | Anomalie non bloccanti | OCR confidence 0.70-0.85, CB HALF_OPEN, device offline <1h |
| ERROR | Errori bloccanti singola operazione | ADB timeout, bet not confirmed, state inconsistent |
| CRITICAL | Sistema o rischio finanziario | Device offline >1h con operazioni scoperte, Netcap auth failed |

Retention: PROD 90 giorni file, 7 giorni Redis Stream.

## 3. Metriche chiave (Prometheus)

| Categoria | Metriche |
|---|---|
| Device | `devices_online_total`, `device_battery_level`, `circuit_breaker_state` |
| ScreenReader | `screen_reads_total{method,result}`, `ocr_confidence`, `screen_read_latency_seconds` |
| TouchInjector | `tap_total{type,result}`, `bet_placement_latency_seconds` |
| Cascade | `cascade_active_total`, `cascade_completed_total{outcome}`, `cascade_recovery_total{result}` |
| Operations | `operations_active_total{status}`, `operations_completed_total{process,result}` |
| HITL | `hitl_pending_total{severity}`, `hitl_resolution_time_seconds` |
| Netcap | `netcap_writes_total{type,result}`, `netcap_cb_state`, `netcap_queue_depth` |

## 4. Dashboard Grafana (6 panel)

1. **Device Farm Overview**: gauge online/total, battery <20%, CB OPEN, uptime %
2. **ScreenReader Performance**: latenza uiautomator vs OCR, success rate, OCR confidence media
3. **Cascade Operations**: attive, completate per outcome, latenza check, ERROR/HITL_BLOCKED
4. **HITL Queue**: pending per severity, tempo medio risoluzione, ultime 20 HITL
5. **Netcap Integration**: CB state, queue depth, latenza scritture, success/fail rate
6. **OpSec**: heatmap operazioni per book per ora, correlation warnings, identità con più operazioni

## 5. Alerting rules (Prometheus)

```yaml
groups:
  - name: devices
    rules:
      - alert: DeviceOffline; expr: device_online_status == 0; for: 5m; severity: warning
      - alert: DeviceOfflineCritical; expr: device_online_status == 0; for: 1h; severity: critical
      - alert: LowBattery; expr: device_battery_level < 0.15; for: 10m; severity: warning
  - name: cascade
    rules:
      - alert: CascadeStuck; expr: cascade_check_staleness_seconds > 900; for: 5m; severity: critical
  - name: netcap
    rules:
      - alert: NetcapCircuitBreakerOpen; expr: netcap_cb_state == 2; for: 2m; severity: critical
      - alert: NetcapQueueDepth; expr: netcap_queue_depth > 50; for: 5m; severity: warning
  - name: hitl
    rules:
      - alert: HITLPendingCritical; expr: hitl_pending{severity="critical"} > 0; for: 1m; severity: critical
```

## 6. Runbook: Device offline

```
1. Verifica dashboard: quante operazioni associate? In che stato?
2. Se nessuna operazione attiva:
   → Verifica fisicamente: caricatore? Schermo?
   → Batteria scarica: collega, attendi boot
   → Non risponde: riavvia forzato (power 10s)
3. Se operazioni in BACK_PLACED o LAY_PLACED:
   → CRITICAL: operazione scoperta
   → Offline <1h: attendi rientro, recovery automatico
   → Offline >1h: accedi manualmente book/Betfair, bancare manualmente
4. Se irrecuperabile:
   → Assegna identità a telefono di replace
   → Recovery cascade da stato reale Betfair
```

## 7. Runbook: OCR confidence bassa

```
1. Verifica: quale book? Sistematico o sporadico?
2. Se sistematico su un book:
   → Possibile cambio layout app
   → Verifica template, registra nuovi screenshot
   → Prova uiautomator dump: se ora funziona, cambia priorità
3. Se sporadico:
   → Possibile schermata animata (loading spinner)
   → Aumenta delay prima di screenshot
   → Aggiungi double-read (leggi 2 volte, confronta)
```

## 8. Runbook: Cascade in ERROR >30min

```
1. Apri trace, identifica ultimo span fallito
2. ADB_TIMEOUT → verify_and_recover_lay
   DEVICE_OFFLINE → runbook device offline
   BET_NOT_CONFIRMED → verifica manuale Betfair
   STATE_INCONSISTENT → leggi stato reale, aggiorna Redis
3. Se scoperta: bancare manualmente, aggiornare state
4. Se non recuperabile: FAILED, registra perdita, post-mortem
```

## 9. Runbook: Netcap unreachable

```
1. Ping netcap-server dal orchestratore
2. Raggiungibile ma API non risponde: verifica credenziali, log
3. Non raggiungibile: contatta Enzo, scritture in coda
4. AUTH_FAILED: CRITICAL, verifica token, aggiorna config
```
