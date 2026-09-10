# Data Model e Entità

```yaml
---
id: ARCH-DATA-MODEL
title: "Data Model — 11 entità principali e relazioni"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 3 (Data Model e entità)
kb_links:
  - P6-RICONCILIAZIONE-PROCEDURA
  - P7-APP-CONVENZIONI-SEGNO
  - P7-APP-CALCOLO-SALDI-PROFITTI
  - P4-BANKROLL-GESTIONE
---
```

## Diagramma delle relazioni

```text
[Device] 1──1 [Identity] 1──N [Account] 1──N [Balance]
                         │         │
                         │         └──N [Operation] 1──1 [CascadeState]
                         │                    │
                         │                    └──N [Rectification]
                         │
                         ├──1 [Credential] (vault)
                         ├──N [Movement]
                         └──N [BonusExpiry]

[Signal] 1──N [Operation]
```

## 1. Identity (Identità/Collaboratore)

```python
class IdentityStatus(Enum):
    ONBOARDING = "onboarding"      # Documenti raccolti, strumenti in creazione
    READY = "ready"                # Hype + PayPal attivi, pronta per apertura book
    ACTIVE = "active"              # Almeno 1 book aperto, operativa
    SUSPENDED = "suspended"        # Temporaneamente sospesa
    CLOSED = "closed"              # Identità chiusa

@dataclass
class Identity:
    id: str                    # "ID-001"
    status: IdentityStatus
    nome: str; cognome: str; data_nascita: date; luogo_nascita: str; codice_fiscale: str
    documento_tipo: str; documento_numero: str; documento_rilascio: str
    documento_data_rilascio: date; documento_data_scadenza: date
    indirizzo: str; citta: str; cap: str
    email: str; telefono: str; iban: str; carta_numero: str; carta_scadenza: str
    device_id: Optional[str]
    hype_consent_expiry: Optional[date]  # Scadenza consenso PSD2 (90 giorni)
    created_at: datetime; updated_at: datetime
```

Note: CVV e documenti non sono in chiaro — vanno nel vault. `device_id` opzionale. `hype_consent_expiry` gestisce rinnovo PSD2 ogni 90 giorni.

## 2. Device (Telefono fisico)

```python
class DeviceStatus(Enum):
    AVAILABLE = "available"; ASSIGNED = "assigned"; OFFLINE = "offline"; MAINTENANCE = "maintenance"

@dataclass
class Device:
    id: str                    # "192.168.1.101:5555"
    host_id: str               # Quale dei 5 host gestisce questo dispositivo
    model: str; android_version: str; screen_resolution: str
    sim_iccid: str; sim_carrier: str; ip_address: str
    battery_level: float       # 0.0-1.0
    status: DeviceStatus
    identity_id: Optional[str]
    last_seen: datetime
```

Note: ogni dispositivo ha SIM propria → IP mobile unico (anti-correlation). `screen_resolution` critico per coordinate tap dinamiche.

## 3. Account (Conto bookmaker)

```python
class AccountStatus(Enum):
    DA_APIRE = "da_aprire"; APERTO = "aperto"; LIMITATO = "limitato"; CHIUSO = "chiuso"

@dataclass
class Account:
    id: str                    # "ACC-ID001-SISAL"
    identity_id: str; book: str; book_family: str
    status: AccountStatus
    date_opened: date          # Per calcolo vincoli di famiglia
    expected_balance: float; real_balance: Optional[float]; last_reconciled: Optional[date]
    is_betfair: bool           # True → differenza in colonna BANCO non BOOK
    notes: Optional[str]       # "BONUS: 30.00" per wagering
```

Vincoli di famiglia: Lottomatica 7gg, Sisal 7gg, E-Play 2gg, Eurobet 2gg, StarCasino 4gg. Si riconciliano solo conti `APERTO` o `LIMITATO`.

## 4. Signal (Segnale Telegram)

```python
class SignalStatus(Enum):
    RECEIVED = "received"; PARSED = "parsed"; TC_VERIFIED = "tc_verified"
    TC_DISCREPANCY = "tc_discrepancy"; ASSIGNED = "assigned"; EXECUTING = "executing"
    COMPLETED = "completed"; EXPIRED = "expired"

@dataclass
class Signal:
    id: str; raw_text: str
    book: str; category: SignalCategory; bonus_type: BonusType; difficulty: str
    strategy: dict; deadline: datetime; priority: str; expected_profit: str
    status: SignalStatus; tc_verified: bool; tc_discrepancies: list
    telegram_message_id: str  # Chiave di idempotenza
```

## 5. Operation (Operazione bonus)

```python
class OperationStatus(Enum):
    PENDING = "pending"; PARAMS_CONFIRMED = "params_confirmed"
    BACK_PLACED = "back_placed"; LAY_PLACED = "lay_placed"
    AUTOPLAY_STARTED = "autoplay_started"; WAITING_RESULT = "waiting_result"
    COMPLETED = "completed"; FAILED = "failed"; CANCELLED = "cancelled"
    HITL_BLOCKED = "hitl_blocked"

@dataclass
class Operation:
    id: str; signal_id: str; identity_id: str; account_id: str
    category: str; bonus_type: str; book: str
    stake_back: float; stake_lay: Optional[float]; liability: Optional[float]
    commission_rate: float     # 0.045 = 4,5%
    profit_book: Optional[float]; profit_banco: Optional[float]; fee_betfair: Optional[float]
    status: OperationStatus; cascade_state: Optional[CascadeState]
    opened_at: datetime; closed_at: Optional[datetime]  # Profitti attribuiti al mese di apertura
    hitl_required: bool; hitl_reason: Optional[str]
    idempotency_key: str       # hash(signal_id + identity_id + book)
```

## 6. CascadeState (Stato cascata sport — in Redis, TTL 48h)

```python
class CascadeStatus(Enum):
    PENDING; EVENT_1_LAYED; EVENT_1_WAITING; EVENT_1_WON; EVENT_1_LOST
    EVENT_2_LAYED; EVENT_2_WAITING; EVENT_2_WON; EVENT_2_LOST
    EVENT_3_LAYED; EVENT_3_WAITING; EVENT_3_WON; EVENT_3_LOST
    COMPLETED; ERROR; HITL_BLOCKED

@dataclass
class CascadeState:
    operation_id: str; identity_id: str; device_id: str
    events: list[CascadeEvent]; current_event_index: int
    state: CascadeStatus
    back_odds: list[float]; lay_odds: list[float]
    lay_stakes: list[float]; liabilities: list[float]
    betfair_bet_ids: list[str]
    last_check_at: datetime; next_check_at: datetime
    retry_count: int; max_retries: int  # default 3
```

## 7. Movement (Movimento finanziario)

```python
class MovementType(Enum):
    DEPOSITO = "deposito"; PRELIEVO = "prelievo"; TRASFERIMENTO = "trasferimento"; COSTO = "costo"

class MovementStatus(Enum):
    PENDING = "pending"; CONFIRMED = "confirmed"; REJECTED = "rejected"; DUPLICATE = "duplicate"

@dataclass
class Movement:
    id: str; identity_id: str
    type: MovementType; source_account: str; dest_account: str
    amount: float  # Positivo; segno applicato dal RulesEngine
    source: str    # "gocardless" | "ocr_paypal" | "manual" | "auto"
    book: Optional[str]; description: Optional[str]; transaction_date: date
    status: MovementStatus
    idempotency_key: str  # hash(identity_id + source_account + amount + transaction_date + type)
    hitl_required: bool
```

Convenzione di segno: Book: deposito aggiunge (+), prelievo sottrae (-). Hype/PayPal: deposito sottrae (-), prelievo aggiunge (+).

## 8. Balance, 9. Rectification, 10. Credential, 11. BonusExpiry

Sintesi: Balance registra saldo letto vs atteso (tolleranza < 1 EUR = arrotondamento). Rectification generata se differenza > 1 EUR, con `column` BOOK o BANCO. Credential nel vault (AES-256-GCM), con `vault_key` di riferimento. BonusExpiry traccia scadenze adesione e completamento.
