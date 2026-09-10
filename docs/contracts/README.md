# Contratti tra moduli — Schema Input/Output

```yaml
---
id: CONTRACTS-ALL
title: "Contratti tra moduli — Schema Input/Output per ogni modulo"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 4 (per esteso)
conventions:
  - "Ogni modulo espone interfaccia Python (metodi) e output contract (dataclass)"
  - "Ogni modulo restituisce Result[T] (Success/Failure + error_code)"
  - "Nessun dict generico: tutto tipizzato"
  - "Error codes sono stringhe UPPER_SNAKE_CASE"
  - "Idempotency: ogni write ha idempotency_key"
---
```

## Convenzioni

1. **Comunicazione sincrona**: i moduli si chiamano via funzioni Python (stesso processo)
2. **Tipizzazione forte**: ogni input/output è dataclass o Pydantic model
3. **Errori espliciti**: `Result[T]` con `success`, `data`, `error`, `error_code`
4. **Idempotenza**: ogni operazione di scrittura ha chiave idempotenza

---

## 1. ScreenReader

### Interfaccia

```python
class ScreenReader:
    def read_screen(self, device_id: str, screen_type: Optional[str] = None) -> Result[ScreenContent]
    def find_element(self, device_id: str, hint: str, field: str = "text") -> Result[UIElement]
    def read_balance(self, device_id: str, book: str) -> Result[BalanceReading]
    def read_bet_status(self, device_id: str, betfair: bool = False) -> Result[BetStatus]
    def read_movements(self, device_id: str, app: str) -> Result[list[MovementReading]]
```

### Output contracts

```python
@dataclass
class ScreenContent:
    elements: list[UIElement]
    screen_type: str          # "balance_page" | "my_bets" | "market_page" | "unknown"
    confidence: float         # 0.0-1.0
    method: str               # "uiautomator" | "ocr"
    screenshot_path: Optional[str]  # salvato se confidence < soglia (per HITL)

@dataclass
class UIElement:
    text: str
    bounds: list[int]         # [x1, y1, x2, y2]
    resource_id: Optional[str]
    class_name: Optional[str]
    confidence: float         # 1.0 per uiautomator

@dataclass
class BalanceReading:
    raw_balance: float
    open_bets: float
    real_bonus: float
    wagering_bonus: Optional[float]
    read_method: str
    confidence: float

@dataclass
class BetStatus:
    status: str               # "matched" | "unsettled" | "settled_won" | "settled_lost"
    lay_odds: Optional[float]
    confidence: float

@dataclass
class MovementReading:
    amount: float
    description: str
    date: str                 # formato grezzo
    direction: str            # "in" | "out"
    confidence: float
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `DEVICE_OFFLINE` | ADB non raggiunge il dispositivo | Retry 3 volte con backoff, poi marca device OFFLINE e alert |
| `ADB_TIMEOUT` | Comando ADB non risponde entro 10s | Retry 1 volta, poi HITL |
| `UIAUTOMATOR_BLOCKED` | App blocca uiautomator (FLAG_SECURE) | Fallback automatico a OCR |
| `OCR_LOW_CONFIDENCE` | PaddleOCR confidence < 0.85 | HITL — invia screenshot all'operatore |
| `SCREEN_NOT_RECOGNIZED` | Template matching / screen classification fallisce | HITL — invia screenshot |
| `NO_ELEMENTS_FOUND` | Nessun elemento trovato con l'hint cercato | HITL o retry con hint alternativo |

### Logica di fallback interna

```python
def read_balance(self, device_id: str, book: str) -> Result[BalanceReading]:
    # TENTATIVO 1: uiautomator
    xml_result = self._uiautomator_dump(device_id)
    if xml_result.success:
        elements = parse_xml(xml_result.data)
        balance = extract_balance_from_elements(elements, book)
        if balance and balance.confidence >= 0.85:
            return Result.ok(balance)  # method="uiautomator", confidence=1.0

    # TENTATIVO 2: screenshot + PaddleOCR
    screenshot = self._screenshot(device_id)
    if not screenshot.success:
        return Result.fail("Screenshot failed", "ADB_TIMEOUT")

    roi = crop_balance_region(screenshot.data, book)
    ocr_result = self._paddleocr.predict(roi)

    if ocr_result.confidence < 0.85:
        path = save_for_hitl(screenshot.data, device_id, book)
        return Result.fail("OCR confidence too low", "OCR_LOW_CONFIDENCE")

    balance = parse_balance_from_ocr(ocr_result, book)
    return Result.ok(balance)  # method="ocr", confidence=ocr_result.confidence
```

---

## 2. TouchInjector

### Interfaccia

```python
class TouchInjector:
    def tap_element(self, device_id: str, hint: str, field: str = "text") -> Result[bool]
    def tap_coordinates(self, device_id: str, x: int, y: int) -> Result[bool]
    def input_text(self, device_id: str, text: str, field_hint: Optional[str] = None) -> Result[bool]
    def navigate_to(self, device_id: str, destination: str, book: str) -> Result[bool]
    def place_back_bet(self, device_id: str, book: str, stake: float,
                       selections: list[dict]) -> Result[BackBetResult]
    def place_lay_bet(self, device_id: str, market: str, odds: float,
                      stake: float) -> Result[LayBetResult]
```

### Output contracts

```python
@dataclass
class BackBetResult:
    confirmed: bool
    bet_id: Optional[str]
    actual_stake: float

@dataclass
class LayBetResult:
    confirmed: bool
    bet_id: Optional[str]
    actual_odds: float       # quota effettiva (può differire da quella richiesta)
    actual_stake: float      # stake effettivamente matchato
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `TAP_MISSED` | Elemento non trovato alle coordinate attese | Retry 2 volte, poi ScreenReader per ritrovare l'elemento |
| `NAVIGATION_FAILED` | Schermata di destinazione non raggiunta dopo N tap | HITL — screenshot per debug |
| `BET_NOT_CONFIRMED` | Book non conferma la scommessa entro 15s | HITL — possibile problema (saldo insufficiente, quote cambiata, limite) |
| `ODDS_CHANGED` | Quota richiesta non più disponibile | Ricalcolo con MultiplierEngine usando la nuova quota |
| `TEXT_INPUT_FAILED` | ADB input text non funziona (campo non focusato) | Retry con tap preliminare sul campo |
| `CAPTCHA_DETECTED` | CAPTCHA appare durante navigazione | 2Captcha API → se fallisce, HITL |

### Humanizzazione

```python
def _human_tap(self, device_id: str, x: int, y: int):
    """Tap con jitter spaziale e temporale"""
    jx = x + random.randint(-5, 5)
    jy = y + random.randint(-5, 5)
    duration = random.randint(50, 200)
    subprocess.run(['adb', '-s', device_id, 'shell', 'input', 'tap', str(jx), str(jy)])
    time.sleep(random.uniform(0.3, 1.5))
```

---

## 3. SignalParser

> Vedi `docs/modules/signal-parser/` per documentazione completa.

### Interfaccia sintetica

```python
class SignalParser:
    def parse(self, raw_text: str, telegram_message_id: str) -> Result[Signal]
    async def parse_and_verify(self, raw_text: str, telegram_message_id: str) -> Result[Signal]
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `LLM_PARSE_FAILED` | Llama 3 non riesce a estrarre dati strutturati | Retry 2 volte con prompt diverso, poi HITL |
| `MISSING_CRITICAL_FIELD` | Campo obbligatorio mancante (book, deadline, amount) | HITL — operatore compila manualmente |
| `DUPLICATE_SIGNAL` | Stesso telegram_message_id già processato | Skip (idempotenza) |
| `LLM_TIMEOUT` | Llama 3 inference > 30s | Retry 1 volta, poi coda di retry differito |

---

## 4. MultiplierEngine

### Interfaccia

```python
class MultiplierEngine:
    def calculate_cascade(self, back_stake: float, back_odds: list[float],
                          lay_odds: list[float], commission: float = 0.045,
                          strategy: str = "equal_profit") -> Result[CascadeCalculation]
    def calculate_single(self, back_stake: float, back_odds: float,
                         lay_odds: float, commission: float = 0.045) -> Result[SingleCalculation]
```

### Output contract

```python
@dataclass
class CascadeCalculation:
    events: list[CascadeEventCalc]  # 3 eventi
    profit_per_scenario: dict       # {"event_1_won": 12.50, ...}
    total_liability: float
    is_valid: bool
    warnings: list[str]

@dataclass
class CascadeEventCalc:
    index: int
    lay_stake: float
    liability: float
    lay_odds: float
    profit_if_won: float
    profit_if_all_lost: float
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `INVALID_ODDS` | Quote <= 1.0 o malformate | Rifiuta il segnale, HITL |
| `NEGATIVE_PROFIT` | Strategia produce profitto negativo in qualche scenario | Segnala come non profittevole, skip |
| `ROUNDING_ERROR` | Errore cumulativo di arrotondamento > 0.50 EUR | Warning (non bloccante) |

---

## 5. CascadeEngine

### Interfaccia

```python
class CascadeEngine:
    def start_cascade(self, operation: Operation,
                      calculation: CascadeCalculation) -> Result[str]
    def check_and_advance(self, cascade_id: str) -> Result[CascadeState]
    def recover_cascade(self, cascade_id: str) -> Result[CascadeState]
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `CASCADE_NOT_FOUND` | cascade_id non in Redis | Log error, skip |
| `DEVICE_BUSY` | Device lock non acquisito | Riprova tra 30s |
| `DEVICE_OFFLINE` | Telefono non raggiungibile | Retry 3 volte, poi HITL_BLOCKED |
| `BET_NOT_FOUND` | Scommessa non trovata su Betfair (My Bets vuoto) | HITL |
| `LAY_PLACEMENT_FAILED` | TouchInjector non riesce a piazzare bancata | HITL — rischio copertura rotta |
| `MAX_RETRIES_EXCEEDED` | 3 retry consecutivi falliti | HITL_BLOCKED |
| `STATE_INCONSISTENT` | Stato Redis non matcha stato reale Betfair | Recovery completo + HITL |
| `ODDS_NEGATIVE_PROFIT` | Ricalcolo produce profitto negativo | HITL |

---

## 6. RulesEngine

### Interfaccia

```python
class RulesEngine:
    def reconcile(self, balance_reading: BalanceReading,
                  expected_balance: float, account: Account) -> Result[ReconciliationResult]
```

### Output contract

```python
@dataclass
class ReconciliationResult:
    adjusted_balance: float      # raw_balance + open_bets + real_bonus
    expected_balance: float
    difference: float            # adjusted_balance - expected_balance
    column: str                   # "BOOK" | "BANCO" (Betfair → BANCO)
    bonus_note: Optional[str]     # "BONUS: 30.00" se wagering bonus presente
    action: str                   # "no_adjustment" | "adjust" | "hitl_required"
    rectification: Optional[Rectification]
    rules_applied: list[str]
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `BALANCE_READ_FAILED` | BalanceReading dal ScreenReader è fallito | Skip questo account, continua con il prossimo |
| `ANOMALOUS_DIFFERENCE` | Differenza > 500 EUR (possibile errore di lettura) | HITL — non rettificare automaticamente |

---

## 7. AppApiClient

### Interfaccia

```python
class AppApiClient:
    def write_operation(self, operation: Operation) -> Result[str]
    def write_movement(self, movement: Movement) -> Result[str]
    def write_rectification(self, rectification: Rectification) -> Result[str]
    def get_expected_balance(self, account_id: str) -> Result[float]
    def get_identity_accounts(self, identity_id: str) -> Result[list[Account]]
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `NETCAP_UNREACHABLE` | Server Netcap non raggiungibile | Retry 3 volte con backoff, poi coda differita |
| `NETCAP_AUTH_FAILED` | Credenziali API non valide | Alert critico — blocca tutte le scritture |
| `NETCAP_VALIDATION_ERROR` | Netcap rifiuta i dati (formato errato) | HITL — logga i dati inviati per debug |
| `NETCAP_TIMEOUT` | Risposta > 10s | Retry 1 volta, poi coda differita |

### Idempotenza

Ogni chiamata `write_*` include l'`idempotency_key` nell'header della richiesta. Se Netcap riceve la stessa chiave due volte, restituisce l'ID della prima operazione senza duplicare.

---

## 8. MovementImporter

### Interfaccia

```python
class MovementImporter:
    def import_hype(self, identity_id: str, date_from: date,
                    date_to: date) -> Result[list[Movement]]
    def import_paypal(self, device_id: str, identity_id: str) -> Result[list[Movement]]
    def check_consent_expiry(self, identity_id: str) -> Result[ConsentStatus]
```

### Error codes

| Codice | Quando | Cosa fa l'orchestratore |
|---|---|---|
| `CONSENT_EXPIRED` | Consenso PSD2 scaduto | Alert HITL, skip import Hype |
| `CONSENT_EXPIRING_SOON` | Consenso scade entro 7 giorni | Alert preventivo |
| `GOCARDLESS_API_ERROR` | API GoCardless restituisce errore | Retry 3 volte, poi coda differita |
| `PAYPAL_OCR_FAILED` | OCR su app PayPal fallisce | HITL — screenshot per lettura manuale |
| `NO_NEW_MOVEMENTS` | Nessun nuovo movimento dall'ultimo import | Normale, non un errore |

---

## 9. RAGSystem

### Interfaccia

```python
class RAGSystem:
    def query(self, question: str, context: dict,
              process: Optional[int] = None,
              book: Optional[str] = None) -> Result[RAGResponse]
    def verify_tc(self, signal: Signal) -> Result[TCVerification]
    def index_document(self, file_path: str) -> Result[bool]
```

### Output contracts

```python
@dataclass
class RAGResponse:
    recommendation: str
    confidence: float
    sources: list[str]         # ID dei documenti recuperati
    action: str                # "auto_execute" | "hitl_confirm" | "no_match"

@dataclass
class TCVerification:
    verified: bool
    discrepancies: list[str]
    matched_docs: list[str]
```

### Decision logic

- confidence >= 0.85 e action == "auto_execute" → esegui automaticamente
- confidence < 0.85 o action == "hitl_confirm" → HITL
- action == "no_match" → HITL con messaggio "Nessun caso simile trovato"

---

## 10. OpSec

### Interfaccia

```python
class OpSec:
    def schedule_operation(self, identity_id: str, operation_type: str,
                           book: str) -> Result[datetime]
    def check_correlation_risk(self, batch: list[Operation]) -> Result[CorrelationCheck]
```

### Output contract

```python
@dataclass
class CorrelationCheck:
    risk_level: str          # "low" | "medium" | "high"
    warnings: list[str]
    blocked: bool            # True se risk=high → operazioni bloccate
```
