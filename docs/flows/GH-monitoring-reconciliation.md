# Flussi G & H — Monitoring, Ops e Riconciliazione manuale

```yaml
---
id: FLOWS-GH-MONITORING
title: "Flussi G & H — Monitoring/Ops e Riconciliazione manuale (fallback)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 21.8 (0a1498cb)
use_cases: [G1-G6, H1-H2]
kb_links:
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
related:
  - docs/flows/use-case-map.md
  - docs/modules/error-handling-system.md
---
```

## Flusso G — Monitoring e ops

**Attore**: Enzo (Head of Ops), Augusto (Tech Lead), Raffaele | **Frequenza**: giornaliero + on-demand

### G1 — Check giornaliero stato sistema
1. Enzo apre dashboard Grafana ogni mattina
2. Panel 1 (Device Farm Overview): tutti telefoni online, batteria carica, nessun circuit breaker OPEN
3. Panel 4 (HITL Queue): nessuna HITL critica in sospeso
4. Se tutto verde → giornata operativa senza interventi

### G2 — Risposta ad alert critico
1. Augusto riceve alert Telegram/SMS `CRITICAL` (es. "Redis connection lost")
2. SSH all'orchestratore → `systemctl restart redis orchestrator`
3. Sistema esegue `recover_all_cascades` all'avvio → riprende operazioni interrotte

### G3 — Gestione Netcap down
1. Circuit breaker Netcap OPEN → scritture in coda Redis
2. Enzo contatta provider o attende rientro
3. Netcap online → `process_netcap_queue()` svuota coda automaticamente → CB si chiude

### G4 — Gestione Redis failure
1. Stato cascade perso → `recover_all_cascades` legge stato reale da Betfair via ScreenReader
2. Se `settled_won` → completa operazione
3. Se `matched`/`unsettled` → HITL_BLOCKED
4. Se `not_found` → HITL_BLOCKED + alert critico (possible exposed position)

### G5 — Post-mortem operazione fallita
1. Operazione in `FAILED` o DLQ
2. Augusto apre trace nella HITL Dashboard → analizza log (structlog JSON) → identifica causa radice
3. Se perdita finanziaria → registra manualmente in Netcap
4. Se necessario → aggiorna KB (Flusso F) per prevenire recidive

### G6 — Review profitti
- Raffaele controlla profitti giornalieri/settimanali nella dashboard → routine

---

## Flusso H — Riconciliazione manuale (fallback)

**Attore**: Raffaele | **Frequenza**: raro (decrescente con maturità sistema)

### H1 — Rettifica manuale (sistema non riesce)
1. ScreenReader restituisce `SCREEN_NOT_RECOGNIZED` o `OCR_LOW_CONFIDENCE` per 3 tentativi consecutivi su book specifico
2. Riconciliazione sospesa → coda HITL
3. Raffaele apre app del book fisicamente (o via scrcpy) → legge saldo reale (es. €120,00)
4. Dashboard → `[Inserimento Manuale Saldo]` → digita `120.00` → `[Conferma]`
5. Sistema accetta dato → `BalanceReading(read_method="hitl_manual")` → RulesEngine completa riconciliazione → scrive in Netcap

### H2 — Verifica post-riconciliazione
- Raffaele fa controllo qualità random su riconciliazioni completate → verifica saldi → conferma correttezza
