# SignalParser — Design Document

```yaml
---
id: MOD-SIGNAL-PARSER-DESIGN
title: "SignalParser — Pipeline di parsing (regex → LLM → postproc → T&C)"
status: active
created: 2026-07-23
updated: 2026-07-23
sources:
  - Capitolo 9 (per esteso)
  - Capitolo 4 (§4.4 — interfaccia)
parent: MOD-SIGNAL-PARSER
---
```

## 1. Formato dei segnali reali

I segnali hanno tre varianti principali:

### Variante 1 — Sport semplice (Stanleybet)

```text
📚 STANLEYBET
💎RICORRENTE SPORT PER TUTTI 🚨 LIVELLO FACILE
✅Strategia operativa: Aderisci alla promo e ricarica 100€,
  scommetti l'intero deposito in tripla, su Champions League,
  Europa League e Conference League, con quota minima 1.8 per evento
  e con eventi che terminano entro 3 giorni
✅A refertazione completa, riceverai 25€ di bonus,
  da rigiocare in tripla a quota 2 per evento, solo sulle coppe europee
⏰Scadenza: 23 Luglio 23:59
🤩🤩 PRIORITÀ MEDIA
💰💰PROFITTO MEDIO:🔡💶
```

### Variante 2 — Casinò semplice (SNAI)

```text
📚 SNAI
💎RICORRENTE SLOT PER TUTTI 🚨 LIVELLO FACILE
✅Attiva la promo e gioca 🔡🔡€ a spin minimi alle slot in promozione,
  ricevi il 10% dell'importo giocato sino a massimo 😳😃€ in bonus Real
✋Slot consigliata: Jackpot Bells 10000
⏰Scadenza: 26 luglio 23:59
⚡️⚡️VARIANZA MEDIA
🤩🤩 PRIORITÀ MEDIA
💰VALORE ATTESO: +🔡💶🚀
```

### Variante 3 — Casinò avanzato (Sisal)

```text
📚SISAL
⏳RICORRENTE SLOT PER TUTTI 🚨 LIVELLO AVANZATO
✅Strategia Operativa: Attiva la Promo.. giocare con saldo reale 🔡🔡€
  a spin minimi alla slot in promozione, si riceve un fun 30€ X40 con cap 30€
📛SLOTO CONSIGLIATA RIGIOCO: King Kong Splash Jackpot King
Strategia:
  Margine: Big Bass Bonanza spin 1€
  Target: 80€
  Rollover: Oink Oink Oink, Stardust oppure Big Bass Bonanza spin 0.10€
⏰Scadenza: 24 Luglio 23:59
🤩🤩🤩 PRIORITA' ALTA
⚡️⚡️MEDIA VARIANZA
💰💰VALORE ATTESO: +🔡💶 🚀
```

## 2. Caratteristiche del formato

| Caratteristica | Dettaglio |
|---|---|
| **Emoji come delimitatori** | 📚 = book, 💎/⏳ = tipo bonus, ✅ = strategia, ⏰ = scadenza, 🤩 = priorità, 💰 = profitto/valore atteso, ⚡️ = varianza |
| **Emoji dinamiche animate** | Su Telegram, gli importi reali (quote, stake, profitto atteso) non sono testo, ma **emoji animate**. Copiando il messaggio come testo, questi valori vengono persi o oscurati (🔡🔡€, 🔡💶). |
| **Struttura semi-ricorrente** | I campi base (book, tipo, scadenza, priorità) sono quasi sempre presenti in testo. I campi strategia variano molto tra sport e casinò. |
| **Linguaggio naturale** | La strategia è scritta in linguaggio naturale italiano, non in formato strutturato. Richiede NLP per l'estrazione. |
| **Campi condizionali** | Il casinò ha campi che lo sport non ha (slot consigliata, target, rollover, varianza) e viceversa (quote minime, eventi, leghe). |

## 3. Pipeline di acquisizione ibrida (Testo + Computer Vision)

Per gestire le emoji animate, l'acquisizione del segnale non usa l'API testuale di Telegram, ma un modulo di acquisizione schermo:

```python
class TelegramAcquirer:
    async def capture_signal(self, device_id: str) -> Result[str]:
        """
        Acquisisce il segnale da Telegram unendo testo e valori CV.
        """
        # 1. Screenshot della chat Telegram sul telefono dell'identità
        screenshot = await self.screen_reader._screenshot(device_id)

        # 2. OCR estrae il testo (delimitatori emoji, descrizione strategia)
        text_ocr = await self.paddle_ocr.predict(screenshot)

        # 3. Computer Vision rileva e classifica le emoji animate
        animated_values = await self.cv_animator_detector.extract_values(screenshot)

        # 4. Merge: sostituisce i placeholder testuali con i valori reali
        merged_text = self._merge_text_and_values(text_ocr, animated_values)

        return Result.ok(merged_text)
```

Con questa pipeline, il Signal Parser riceve un testo pulito e completo (es. "ricarica 100€" invece di "ricarica 🔡🔡€"), permettendo al modulo LLM di estrarre i parametri correttamente.

## 4. Step 1 — Pre-processing regex

```python
import re

class SignalPreprocessor:
    EMOJI_MAP = {
        "📚": "book_marker",
        "💎": "bonus_type_1",
        "⏳": "bonus_type_2",
        "✅": "strategy",
        "✋": "slot_advised",
        "📛": "slot_rigioco",
        "⏰": "deadline",
        "🤩": "priority",
        "💰": "profit",
        "⚡️": "variance",
    }

    def preprocess(self, raw_text: str) -> dict:
        fields = {}

        book_match = re.search(r'📚\s*(.+?)(?:\n|$)', raw_text)
        fields["book_raw"] = book_match.group(1).strip() if book_match else None

        bonus_match = re.search(r'[💎⏳]\s*(.+?)(?:🚨|\n)', raw_text)
        fields["bonus_type_raw"] = bonus_match.group(1).strip() if bonus_match else None

        diff_match = re.search(r'🚨\s*LVello\s+(\w+)', raw_text, re.IGNORECASE)
        fields["difficulty_raw"] = diff_match.group(1).strip() if diff_match else None

        deadline_match = re.search(r'⏰Scadenza:\s*(.+?)(?:\n|$)', raw_text)
        fields["deadline_raw"] = deadline_match.group(1).strip() if deadline_match else None

        priority_match = re.search(r'🤩+\s*(.+?)(?:\n|$)', raw_text)
        fields["priority_raw"] = priority_match.group(1).strip() if priority_match else None

        profit_match = re.search(r'💰+\s*(.+?)(?:\n|$)', raw_text)
        fields["profit_raw"] = profit_match.group(1).strip() if profit_match else None

        variance_match = re.search(r'⚡️+\s*(.+?)(?:\n|$)', raw_text)
        fields["variance_raw"] = variance_match.group(1).strip() if variance_match else None

        strategy_match = re.search(r'✅\s*(.+?)(?=\n[✋📛⏰🤩💰⚡️]|\Z)', raw_text, re.DOTALL)
        fields["strategy_raw"] = strategy_match.group(1).strip() if strategy_match else None

        slot_match = re.search(r'✋\s*(.+?)(?:\n|$)', raw_text)
        fields["slot_advised_raw"] = slot_match.group(1).strip() if slot_match else None

        rigioco_match = re.search(r'📛\s*(.+?)(?:\n|$)', raw_text)
        fields["slot_rigioco_raw"] = rigioco_match.group(1).strip() if rigioco_match else None

        return fields
```

## 5. Step 2 — LLM extraction con Llama 3 8B

L'LLM riceve il testo pre-processato ed estrae la struttura JSON usando few-shot prompting con esempi di segnali sport e casinò.

```python
class LlamaSignalExtractor:
    SYSTEM_PROMPT = """Sei un estrattore strutturato di segnali di matched betting.
Ricevi il testo di un messaggio Telegram e devi estrarre un JSON strutturato.
Regole:
- Estrai SOLO i campi presenti nel testo. Se un campo non è presente, metti null.
- Le date vanno in formato ISO 8601 (YYYY-MM-DDTHH:MM:SS). Se manca l'anno, usa 2026.
- Le quote sono decimali (es. 1.8, non 1,8).
- Non inventare informazioni non presenti nel testo."""
```

### Output JSON (esempio sport)

```json
{
  "book": "Stanleybet",
  "category": "sport",
  "bonus_type": "ricorrente",
  "difficulty": "facile",
  "strategy": {
    "action": "aderisci_promo_e_ricarica",
    "amount": 100.0,
    "bet_type": "tripla",
    "min_odds": 1.8,
    "events_window_days": 3,
    "leagues": ["Champions League", "Europa League", "Conference League"]
  },
  "bonus": {
    "type": "free_bet",
    "amount": 25.0,
    "wagering": null,
    "reuse": {
      "bet_type": "tripla",
      "min_odds": 2.0,
      "leagues": ["coppe europee"]
    }
  },
  "deadline": "2026-07-23T23:59:00",
  "priority": "media",
  "expected_profit": "medio"
}
```

## 6. Step 3 — Post-processing e validazione

```python
class SignalPostprocessor:
    BOOK_NORMALIZATION = {
        "stanleybet": "Stanleybet",
        "snai": "SNAI",
        "sisal": "Sisal",
        "eurobet": "Eurobet",
        "goldbet": "Goldbet",
        "lottomatica": "Lottomatica",
        "planetwin": "Planetwin",
        "pokerstars": "Pokerstars",
        "bwin": "Bwin",
        "bet365": "Bet365",
        "betfair": "Betfair",
    }

    def postprocess(self, extracted: dict, preprocessed: dict) -> Result[Signal]:
        # 1. Normalizza nome book
        book_raw = (extracted.get("book") or preprocessed.get("book_raw") or "").lower()
        book = self.BOOK_NORMALIZATION.get(book_raw, book_raw.title())
        if not book:
            return Result.fail("Cannot determine book", "MISSING_CRITICAL_FIELD")

        # 2. Determina category (sport/casino)
        category = extracted.get("category")
        if not category:
            if any(kw in preprocessed.get("strategy_raw", "").lower()
                   for kw in ["tripla", "quota", "evento", "scommessa"]):
                category = "sport"
            elif any(kw in preprocessed.get("strategy_raw", "").lower()
                     for kw in ["slot", "spin", "rollover", "wagering"]):
                category = "casino"
            else:
                return Result.fail("Cannot determine category", "MISSING_CRITICAL_FIELD")

        # 3. Parse deadline (formato italiano: "23 Luglio 23:59")
        deadline_raw = preprocessed.get("deadline_raw", "")
        deadline = self._parse_deadline(deadline_raw)
        if not deadline:
            return Result.fail(f"Cannot parse deadline: {deadline_raw}", "MISSING_CRITICAL_FIELD")

        # 4. Normalize bonus_type
        bonus_type = extracted.get("bonus_type", "").lower()
        if "ricorrente" in bonus_type:
            bonus_type = "ricorrente"
        elif "benvenuto" in bonus_type:
            bonus_type = "benvenuto"
        elif "esclusi" in bonus_type:
            bonus_type = "esclusivo"
        elif "rimborso" in bonus_type or "cashback" in bonus_type:
            bonus_type = "rimborso_slot"

        # 5. Build Signal object
        signal = Signal(...)

        # 6. Add casino-specific fields
        if category == "casino":
            signal.strategy["slot_advised"] = extracted.get("slot_advised")
            signal.strategy["slot_rigioco"] = extracted.get("slot_rigioco")
            signal.strategy["casino_strategy"] = extracted.get("casino_strategy")
            signal.strategy["variance"] = extracted.get("variance", "media")

        return Result.ok(signal)

    def _parse_deadline(self, raw: str) -> Optional[datetime]:
        """Parse di date in formato italiano: '23 Luglio 23:59' → datetime"""
        months = {
            "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
            "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
            "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
            "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
            "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12
        }
        match = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{1,2}):(\d{2})', raw, re.IGNORECASE)
        if match:
            day, month_name, hour, minute = match.groups()
            month = months.get(month_name.lower())
            if month:
                return datetime(2026, month, int(day), int(hour), int(minute))
        return None
```

## 7. Step 4 — T&C verification (integrazione RAGSystem)

```python
class SignalParser:
    async def parse_and_verify(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        # 1. Pre-processing
        preprocessed = self.preprocessor.preprocess(raw_text)
        preprocessed["raw_text"] = raw_text
        preprocessed["telegram_message_id"] = telegram_message_id

        # 2. Idempotency check
        existing = await self.db.query(Signal).filter(
            Signal.telegram_message_id == telegram_message_id
        ).first()
        if existing:
            return Result.fail("Signal already parsed", "DUPLICATE_SIGNAL")

        # 3. LLM extraction
        extracted = await self.llama_extractor.extract(preprocessed, raw_text)
        if not extracted.success:
            return extracted

        # 4. Post-processing
        signal_result = self.postprocessor.postprocess(extracted.data, preprocessed)
        if not signal_result.success:
            return signal_result

        signal = signal_result.data

        # 5. T&C verification via RAG
        tc_result = await self.rag_system.verify_tc(signal)
        if tc_result.success:
            verification = tc_result.data
            signal.tc_verified = verification.verified
            signal.tc_discrepancies = verification.discrepancies
            if not verification.verified:
                signal.status = SignalStatus.TC_DISCREPANCY
            else:
                signal.status = SignalStatus.TC_VERIFIED

        # 6. Persist
        await self.db.add(signal)
        await self.db.commit()

        return Result.ok(signal)
```

## 8. Edge cases

| Edge case | Cosa succede |
|---|---|
| Segnale senza book identificabile | `MISSING_CRITICAL_FIELD` → HITL |
| Segnale senza scadenza | `MISSING_CRITICAL_FIELD` → HITL |
| Segnale senza chiara distinzione sport/casino | `MISSING_CRITICAL_FIELD` → HITL |
| LLM produce JSON malformato | `LLM_PARSE_FAILED` → retry 2 volte, poi HITL |
| LLM estrae campi non richiesti | Post-processing ignora campi extra |
| Book non nella mappa di normalizzazione | Usa `book_raw.title()` come fallback |
| Data con anno diverso da 2026 | Si assume 2026 (validazione con Raffaele) |
| Valori oscurati (🔡💶) se CV fallisce | `"OBSCURED"` → HITL per input manuale |
| Messaggio Telegram duplicato | `DUPLICATE_SIGNAL` → skip (idempotenza) |
