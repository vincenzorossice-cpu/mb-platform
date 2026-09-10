# Error Handling & Recovery — System-Wide Patterns

```yaml
---
id: SYS-ERROR-HANDLING
title: "Error Handling & Recovery — Three-Level Failure Model"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 6 (per esteso)
  - Capitolo 4 (error codes per modulo)
applies_to:
  - ScreenReader
  - TouchInjector
  - SignalParser
  - MultiplierEngine
  - CascadeEngine
  - RulesEngine
  - AppApiClient
  - MovementImporter
  - RAGSystem
  - OpSec
---
```

## 1. Principio fondamentale

Un'operazione finanziaria non deve mai trovarsi in uno stato inconsistente. Se il sistema crasha, il telefono va offline, o ADB droppa a metà di un tap, il sistema deve poter determinare esattamente in che stato si trova l'operazione e riprendere da lì — o fermarsi in modo sicuro e allertare un umano.

## 2. Tre livelli di fallimento

| Livello | Esempio | Impatto | Strategia |
|---|---|---|---|
| **L1 — Transient** | ADB timeout momentaneo, rete WiFi lag, OCR confidence bassa | Operazione ritardata | Retry automatico con backoff |
| **L2 — Recoverable** | Telefono offline per 10 min, app crasha, schermata inaspettata | Operazione sospesa, stato preservato | Recovery automatico al rientro del dispositivo |
| **L3 — Critical** | Telefono irraggiungibile >1h, stato Redis perso, scommessa piazzata sul book ma non bancata su Betfair | Rischio finanziario (copertura rotta) | HITL immediato, blocco operazione, alert critico |

## 3. Checkpoint finanziari (state machine)

```text
PENDING          → checkpoint_0: operazione creata, nulla ancora fatto
PARAMS_CONFIRMED → checkpoint_1: parametri confermati da HITL
BACK_PLACED      → checkpoint_2: scommessa back piazzata sul book ← PUNTO DI NON RITORNO
LAY_PLACED       → checkpoint_3: prima bancata piazzata su Betfair
WAITING_RESULT   → checkpoint_4: in attesa del risultato, cascata in gestione
COMPLETED        → checkpoint_5: operazione conclusa, registrata in Netcap
```

**Il checkpoint critico è `BACK_PLACED`**: una volta piazzata la scommessa sul book, il sistema **deve** bancare su Betfair. Se fallisce, l'operazione è scoperta.

## 4. Categorie di errore e strategie

### 4.1 — Device offline

**Trigger**: `DeviceManager.check_device_online()` restituisce `False` per 3 heartbeat consecutivi (>3 minuti).

**Cosa succede**:
- Tutte le operazioni attive su quel dispositivo vengono messe in pausa
- Il `Device.lock` Redis viene mantenuto (non rilasciato) per prevenire race conditions
- Se l'operazione è in stato `BACK_PLACED` o successivo → alert L2 Recoverable
- Se l'operazione è in stato `WAITING_RESULT` con cascata in `EVENT_N_WAITING` → alert L2, la cascata riprenderà quando il dispositivo rientra

**Recovery automatico**:
```python
class DeviceManager:
    async def on_device_back_online(self, device_id: str):
        """Chiamato quando un dispositivo torna online dopo essere stato offline"""
        if not await self.check_device_online(device_id):
            return  # Falso positivo

        suspended_ops = await self.db.query(Operation).filter(
            Operation.device_id == device_id,
            Operation.status.in_([OperationStatus.BACK_PLACED,
                                  OperationStatus.LAY_PLACED,
                                  OperationStatus.WAITING_RESULT])
        ).all()

        for op in suspended_ops:
            await self.cascade_engine.recover_cascade(op.cascade_state_id)
```

**Timeout L3**: se il dispositivo è offline da >1 ora e ha operazioni in `BACK_PLACED` o `LAY_PLACED`:
- Operazione → `HITL_BLOCKED`
- Alert critico: `"CRITICAL: Operation {op.id} exposed — device {device.id} offline since {device.last_seen}"`

### 4.2 — ADB drop a metà operazione

**Scenario critico**: ADB drop durante il piazzamento di una bancata su Betfair — il tap è stato inviato ma non sappiamo se la scommessa è stata confermata.

**Strategia — Verify-then-Act** (mai ritentare ciecamente):
```python
async def _verify_and_recover_lay(self, device_id, market, odds, stake) -> Result[LayBetResult]:
    """Recovery dopo ADB timeout durante piazzamento bancata"""
    await asyncio.sleep(5)  # Stabilizza

    if not await self.device_manager.check_device_online(device_id):
        return Result.fail("Device offline during lay placement", "DEVICE_OFFLINE_LAY")

    bet_status = await self.screen_reader.read_bet_status(device_id, betfair=True)

    if not bet_status.success:
        return Result.fail("Cannot verify bet status after ADB timeout", "VERIFY_FAILED")

    if bet_status.data.status in ["matched", "unsettled"]:
        # La bancata è stata piazzata!
        return Result.ok(LayBetResult(
            confirmed=True,
            actual_odds=bet_status.data.lay_odds or odds,
            actual_stake=stake
        ))

    if bet_status.data.status == "not_found":
        # La bancata NON è stata piazzata. Sicuro ritentare.
        return Result.fail("Lay not placed (verified), safe to retry", "LAY_NOT_PLACED_RETRY_SAFE")

    # Stato ambiguo
    return Result.fail("Ambiguous bet status after ADB timeout", "AMBIGUOUS_STATE")
```

**Regola d'oro**: dopo un ADB timeout su un'operazione finanziaria, **mai ritentare ciecamente**. Sempre verificare lo stato reale via ScreenReader prima di decidere.

### 4.3 — Cascata interrotta (crash orchestratore, Redis failure)

**Recovery da stato reale** (Redis ha perso lo stato):
- Legge stato reale da Betfair via ScreenReader
- Se `settled_won` → completa operazione
- Se `settled_lost` → HITL_BLOCKED (cascade at unknown event)
- Se `matched`/`unsettled` → HITL_BLOCKED (bet still active)
- Se `not_found` → HITL_BLOCKED + alert critico (possible exposed position)

### 4.4 — Tap andato a vuoto

```python
async def tap_element(self, device_id: str, hint: str, field: str = "text") -> Result[bool]:
    # 1. Trova elemento via ScreenReader
    element = await self.screen_reader.find_element(device_id, hint, field)

    if not element.success:
        # Retry 1: reidentifica schermata
        screen = await self.screen_reader.read_screen(device_id)
        if screen.data.screen_type != self._expected_screen:
            nav = await self.navigate_to(device_id, self._expected_screen, self._current_book)
            if not nav.success:
                return Result.fail("Cannot navigate to expected screen", "NAVIGATION_FAILED")
            element = await self.screen_reader.find_element(device_id, hint, field)
            if not element.success:
                return Result.fail("Element still not found after navigation", "TAP_MISSED")
        else:
            return Result.fail("Element not found on correct screen", "TAP_MISSED")

    # 2. Tap con coordinate dell'elemento
    bounds = element.data.bounds
    cx = (bounds[0] + bounds[2]) // 2
    cy = (bounds[1] + bounds[3]) // 2

    await self._human_tap(device_id, cx, cy)

    # 3. Verifica che il tap abbia avuto effetto (solo per tap critici)
    if self._verify_after_tap:
        await asyncio.sleep(1.0)
        post_screen = await self.screen_reader.read_screen(device_id)
        if not self._expected_change(post_screen, self._pre_tap_screen):
            return Result.fail("Tap did not produce expected screen change", "TAP_NO_EFFECT")

    return Result.ok(True)
```

### 4.5 — Quote cambiate durante la cascata

Se `ScreenReader.read_bet_status()` legge una quota lay su Betfair diversa da quella usata nel calcolo (differenza > 0.05):
1. Ricalcola con `MultiplierEngine` usando la nuova quota
2. Se il nuovo profitto è negativo → `ODDS_NEGATIVE_PROFIT` → HITL
3. Se il profitto è ancora positivo → aggiorna stake e procedi

## 5. Retry policies

```python
@dataclass
class RetryPolicy:
    max_retries: int
    backoff_base: float
    backoff_max: float
    backoff_multiplier: float
    retry_on_errors: list[str]

    def get_delay(self, attempt: int) -> float:
        delay = self.backoff_base * (self.backoff_multiplier ** attempt)
        return min(delay, self.backoff_max)
```

| Operazione | Max retries | Backoff | Retry on | Non retry on |
|---|---|---|---|---|
| `ScreenReader.read_screen` | 3 | 1s, 2s, 4s | `ADB_TIMEOUT`, `UIAUTOMATOR_BLOCKED` (fallback OCR) | `DEVICE_OFFLINE`, `OCR_LOW_CONFIDENCE` |
| `ScreenReader.read_balance` | 2 | 2s, 4s | `ADB_TIMEOUT` | `DEVICE_OFFLINE`, `OCR_LOW_CONFIDENCE` |
| `TouchInjector.tap_element` | 2 | 1s, 2s | `TAP_MISSED` (con reidentificazione) | `TAP_NO_EFFECT`, `NAVIGATION_FAILED` |
| `TouchInjector.place_lay_bet` | 1 | 5s | `LAY_NOT_PLACED_RETRY_SAFE` (dopo verify) | `BET_NOT_CONFIRMED`, `AMBIGUOUS_STATE` |
| `AppApiClient.write_operation` | 5 | 2s, 4s, 8s, 16s, 32s | `NETCAP_UNREACHABLE`, `NETCAP_TIMEOUT` | `NETCAP_AUTH_FAILED`, `NETCAP_VALIDATION_ERROR` |
| `MovementImporter.import_hype` | 3 | 5s, 10s, 20s | `GOCARDLESS_API_ERROR` | `CONSENT_EXPIRED`, `NO_NEW_MOVEMENTS` |
| `SignalParser.parse` | 2 | 3s, 6s | `LLM_TIMEOUT` | `LLM_PARSE_FAILED`, `MISSING_CRITICAL_FIELD` |
| `CascadeEngine.check_and_advance` | 3 | 30s, 60s, 120s | `ADB_TIMEOUT`, `DEVICE_OFFLINE` (se recovery possibile) | `BET_NOT_FOUND`, `MAX_RETRIES_EXCEEDED` |

## 6. Circuit breaker

### 6.1 — Per dispositivo

| Parametro | Valore |
|---|---|
| `failure_threshold` | 5 fallimenti consecutivi → OPEN |
| `recovery_timeout` | 300 secondi (5 min) in OPEN prima di HALF_OPEN |
| `half_open_max_calls` | 1 chiamata di test in HALF_OPEN |

Stati: `CLOSED` → (5 failures) → `OPEN` → (5 min) → `HALF_OPEN` → (success) → `CLOSED` o (failure) → `OPEN`

### 6.2 — Per Netcap

Quando il circuit breaker di Netcap è `OPEN`:
- Le scritture vengono messe in coda su Redis Stream `netcap:write_queue`
- Il `QueueProcessor` processa la coda quando il CB torna `HALF_OPEN`/`CLOSED`

## 7. Watchdog e timeout

### 7.1 — Cascade watchdog

Verifica che tutte le cascate attive stiano facendo progressi:
- Se `last_check_at` > 15 min fa e dispositivo online → `check_and_advance`
- Se cascade in `ERROR` state per > 30 min → `HITL_BLOCKED` + alert critico

### 7.2 — Timeout per operazione

| Operazione | Timeout | Cosa succede al timeout |
|---|---|---|
| `uiautomator dump` | 10s | → fallback OCR |
| `screencap` | 10s | → retry 1 volta, poi HITL |
| `tap_element` | 5s (ADB) + 2s (verify) | → retry 2 volte, poi HITL |
| `place_lay_bet` (intera sequenza) | 60s | → verify_and_recover_lay |
| `place_back_bet` (intera sequenza) | 90s | → verify_back_bet |
| `read_balance` | 15s | → retry 2, poi skip account |
| `write_operation` (Netcap) | 10s | → retry 5 con backoff, poi coda |
| `cascade check_and_advance` | 45s | → retry 3, poi HITL_BLOCKED |
| `LLM inference` (Signal Parser) | 30s | → retry 2, poi HITL |

## 8. Dead letter queue

| Entità | Quando | Severity |
|---|---|---|
| Operation | `MAX_RETRIES_EXCEEDED` durante piazzamento | Critical (se BACK_PLACED) |
| Operation | `AMBIGUOUS_STATE` dopo ADB timeout su lay | Critical |
| Operation | `STATE_INCONSISTENT` in cascade recovery | Critical |
| Movement | `DUPLICATE` con dati divergenti | Warning |
| Rectification | `ANOMALOUS_DIFFERENCE` (>500 EUR) | Warning |
| Signal | `MISSING_CRITICAL_FIELD` | Warning |

## 9. Matrice di decisione: cosa fare per ogni errore

| Errore | Durante cosa | Stato operazione dopo | Azione automatica | HITL? |
|---|---|---|---|---|
| `DEVICE_OFFLINE` | read_balance | Invariato | Skip account, continua con prossimo | No |
| `DEVICE_OFFLINE` | check_and_advance (cascade) | Invariato | Retry 3 volte (30s, 60s, 120s), poi pausa | No (se <1h) |
| `DEVICE_OFFLINE` | place_lay_bet | `BACK_PLACED` (scoperto!) | **Niente retry** — attendi rientro, poi verify_and_recover | **Sì se >1h** |
| `ADB_TIMEOUT` | tap_element | Invariato | Retry 2 volte con reidentificazione | No |
| `ADB_TIMEOUT` | place_lay_bet | Incerto | verify_and_recover_lay (leggi Betfair) | Sì se ambiguo |
| `BET_NOT_CONFIRMED` | place_lay_bet | `BACK_PLACED` | HITL — possibile quota cambiata o saldo insufficiente | **Sì** |
| `BET_NOT_CONFIRMED` | place_back_bet | `PENDING` | Retry 1 volta, poi HITL | **Sì** |
| `ODDS_CHANGED` | place_lay_bet | Invariato | Ricalcola con MultiplierEngine, riprova | No (se profitto ancora positivo) |
| `ODDS_NEGATIVE_PROFIT` | place_lay_bet | `BACK_PLACED` | HITL — operazione non più profittevole | **Sì** |
| `OCR_LOW_CONFIDENCE` | read_balance | Invariato | HITL con screenshot | Sì |
| `SCREEN_NOT_RECOGNIZED` | navigate_to | Invariato | HITL con screenshot | Sì |
| `CAPTCHA_DETECTED` | qualsiasi navigazione | Invariato | 2Captcha API → se fallisce, HITL | Sì se 2Captcha fallisce |
| `NETCAP_UNREACHABLE` | write_operation | Invariato | Coda differita, CB OPEN | No |
| `NETCAP_AUTH_FAILED` | qualsiasi scrittura | Bloccata | Alert critico, blocca tutte le scritture | **Sì** |
| `MAX_RETRIES_EXCEEDED` | check_and_advance | `HITL_BLOCKED` | DLQ + alert | **Sì** |
| `AMBIGUOUS_STATE` | place_lay_bet (recovery) | `HITL_BLOCKED` | DLQ critical + alert | **Sì** |
| `STATE_INCONSISTENT` | cascade recovery | `HITL_BLOCKED` | DLQ critical + alert | **Sì** |

## 10. HITL alerting

### Canali di notifica

| Severity | Canale | Latenza target |
|---|---|---|
| `info` | Solo dashboard (WebSocket) | <1s |
| `warning` | Dashboard + Telegram bot | <5s |
| `critical` | Dashboard + Telegram + chiamata/SMS | <30s |

### SLA di risposta HITL

| Severity | Tempo massimo di risposta | Cosa succede se non risposto |
|---|---|---|
| `info` | Nessun SLA | L'operazione resta in coda |
| `warning` | 2 ore | L'operazione viene cancellata, alert ripetuto |
| `critical` | 30 minuti | L'operazione resta bloccata, alert escalation a Enzo, tutte le operazioni simili vengono messe in pausa |
