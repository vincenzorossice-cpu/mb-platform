import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.touch_injector.injector import TouchInjector

logger = logging.getLogger(__name__)


@dataclass
class CasinoPhaseResult:
    """Esito di una singola fase della promo (real / fun / real_bonus)."""
    phase: str  # "real" | "fun" | "real_bonus"
    slot_name: str
    initial_balance: float
    final_balance: float
    target_amount: float
    played_amount: float
    spins_done: int
    spins_needed: int
    completed: bool
    bonus_received: Optional[float] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class CasinoResult:
    """Esito complessivo dell'esecuzione della promo casino (fino a 3 fasi)."""
    book: str
    collaboratore_id: str
    promo_name: str
    phases: list[CasinoPhaseResult]
    total_profit: float
    initial_balance: float
    final_balance: float
    completed: bool


class SlotOCRReader:
    """Legge saldo e stato della slot via EasyOCR su screenshot (Canvas/WebGL,
    illeggibile da uiautomator). Lazy init: il primo readtext e' lento perche'
    carica i pesi del modello, poi il Reader resta cache in self._ocr."""

    def __init__(self):
        self._ocr = None

    def _get_ocr(self):
        if self._ocr is None:
            import easyocr
            self._ocr = easyocr.Reader(['it', 'en'], gpu=False)
        return self._ocr

    def read_slot_state(self, screenshot_path: str) -> dict:
        """Legge lo stato della slot da uno screenshot.

        Returns:
        {
            "saldo": float or None,
            "posta": float or None,
            "vincita": float or None,
            "spin_button_coords": (x, y) or None,
            "is_active": bool,  # True se "SCOMMETTI ATTIVO"
            "raw_texts": [(text, coords, confidence), ...]
        }
        """
        ocr = self._get_ocr()
        results = ocr.readtext(screenshot_path)

        state = {
            "saldo": None,
            "posta": None,
            "vincita": None,
            "spin_button_coords": None,
            "is_active": False,
            "raw_texts": [],
        }

        saldo_coords = None
        posta_coords = None
        vincita_coords = None

        for detection in results:
            bbox = detection[0]
            text = detection[1].strip()
            conf = detection[2]

            if conf < 0.3:
                continue

            x = int(bbox[0][0])
            y = int(bbox[0][1])
            state["raw_texts"].append((text, (x, y), conf))

            text_upper = text.upper()

            if "SALDO" in text_upper:
                saldo_coords = (x, y)

            if "POSTA" in text_upper:
                posta_coords = (x, y)

            if "VINCITA" in text_upper:
                vincita_coords = (x, y)

            if "SPIN" in text_upper and "ATTIVO" not in text_upper:
                state["spin_button_coords"] = (x + 50, y + 20)  # centro approssimativo

            if "ATTIVO" in text_upper and "SCOMMETTI" in text_upper:
                state["is_active"] = "NON" not in text_upper

            amount = self._extract_amount(text)
            if amount is not None:
                if saldo_coords is not None and abs(y - saldo_coords[1]) < 50:
                    state["saldo"] = amount
                elif posta_coords is not None and abs(y - posta_coords[1]) < 50:
                    state["posta"] = amount
                elif vincita_coords is not None and abs(y - vincita_coords[1]) < 50:
                    state["vincita"] = amount

        return state

    def _extract_amount(self, text: str) -> Optional[float]:
        """Estrae un importo numerico da un testo OCR (es. '114,36 €', '5,000.00')."""
        match = re.search(r'(\d[\d.,]*\d)', text)
        if not match: return None
        clean = match.group(1)
        
        # Determina formato in base all'ultimo separatore
        last_comma = clean.rfind(',')
        last_dot = clean.rfind('.')
        
        if last_comma > last_dot:
            # Formato europeo: 1.234,56 o 114,36
            return float(clean.replace('.', '').replace(',', '.'))
        elif last_dot > last_comma:
            # Formato americano: 5,000.00 o 1.50
            return float(clean.replace(',', ''))
        elif ',' in clean:
            # Solo virgola: europeo 114,36
            return float(clean.replace(',', '.'))
        
        # Solo punto o nessuno: intero o americano 1.50
        return float(clean)


class CasinoFlow:
    """Automatizza l'esecuzione di una promo casino (slot) in 3 fasi: giocata
    real (sblocca fun bonus), giocata fun bonus (converte in real bonus),
    giocata real bonus (riscuoti). Chiama solo ScreenReader/TouchInjector
    gia' esistenti; la navigazione verso la slot (apertura promo/book) resta
    a carico del chiamante o di un adapter book-specific non ancora scritto."""

    def __init__(self, screen_reader: ScreenReader, touch_injector: TouchInjector):
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.ocr_reader = SlotOCRReader()
        self._stop_requested = False

    def execute_promo(
        self,
        device_id: str,
        collaboratore_id: str,
        book: str,
        promo_config: dict,
    ) -> Result[CasinoResult]:
        """Esegue una promo casino completa in 3 fasi.

        promo_config = {
            "slot_real": "Lawn n' Complete Disorder",  # slot per fase 1
            "slot_fun": "Big Bass Bonanza",             # slot per fase 2
            "slot_real_bonus": "Oink Oink Oink",        # slot per fase 3
            "target_real": 30.0,          # giocato per sbloccare fun bonus
            "target_fun_balance": 80.0,   # target di saldo nella fase 2
            "fun_rollover_multiplier": 40,  # X40
            "fun_cap": 30.0,               # cap del fun bonus
            "real_bonus_amount": 30.0,     # importo real bonus da rigiocare
            "spin_real": 0.10,             # spin minimo fase 1
            "spin_fun": 1.0,               # spin fase 2 (prima del target)
            "spin_fun_rollover": 0.10,     # spin minimo fase 2 (dopo target)
            "spin_real_bonus": 0.10,       # spin minimo fase 3
        }
        """
        self._stop_requested = False
        phases: list[CasinoPhaseResult] = []

        initial = self.screen_reader.read_balance(device_id, book)
        if not initial.success:
            return Result.fail(f"Cannot read initial balance: {initial.error}", "BALANCE_READ_FAILED")
        initial_balance = initial.data.raw_balance

        # === FASE 1: Giocata Real ===
        logger.info("=== FASE 1: Giocata Real ===")
        phase1 = self._execute_phase(
            device_id=device_id,
            slot_name=promo_config["slot_real"],
            target_amount=promo_config["target_real"],
            spin_amount=promo_config["spin_real"],
            phase_name="real",
        )
        phases.append(phase1)

        if self._stop_requested or not phase1.completed:
            return self._build_result(book, collaboratore_id, promo_config, phases, initial_balance)

        self._exit_slot(device_id)
        time.sleep(3)
        saldi = self._read_all_balances(device_id, book)

        fun_bonus = saldi.get("wagering_bonus")
        if not fun_bonus or fun_bonus <= 0:
            logger.warning("Fun bonus non ricevuto. Promo fallita.")
            return self._build_result(book, collaboratore_id, promo_config, phases, initial_balance)

        logger.info("Fun bonus ricevuto: %.2f EUR", fun_bonus)

        # === FASE 2: Giocata Fun Bonus ===
        logger.info("=== FASE 2: Giocata Fun Bonus ===")
        rollover_target = fun_bonus * promo_config["fun_rollover_multiplier"]
        if promo_config.get("fun_cap"):
            rollover_target = min(rollover_target, promo_config["fun_cap"] * promo_config["fun_rollover_multiplier"])

        phase2 = self._execute_fun_phase(
            device_id=device_id,
            slot_name=promo_config["slot_fun"],
            target_balance=promo_config["target_fun_balance"],
            rollover_target=rollover_target,
            spin_high=promo_config["spin_fun"],
            spin_low=promo_config["spin_fun_rollover"],
            fun_bonus_amount=fun_bonus,
        )
        phases.append(phase2)

        if self._stop_requested or not phase2.completed:
            return self._build_result(book, collaboratore_id, promo_config, phases, initial_balance)

        self._exit_slot(device_id)
        time.sleep(3)
        saldi = self._read_all_balances(device_id, book)

        real_bonus = saldi.get("real_bonus") or 0.0
        if real_bonus <= 0:
            logger.warning("Real bonus non ricevuto. Promo fallita.")
            return self._build_result(book, collaboratore_id, promo_config, phases, initial_balance)

        logger.info("Real bonus ricevuto: %.2f EUR", real_bonus)

        # === FASE 3: Giocata Real Bonus ===
        logger.info("=== FASE 3: Giocata Real Bonus ===")
        phase3 = self._execute_phase(
            device_id=device_id,
            slot_name=promo_config["slot_real_bonus"],
            target_amount=real_bonus,  # rigiocare 1x
            spin_amount=promo_config["spin_real_bonus"],
            phase_name="real_bonus",
        )
        phases.append(phase3)

        self._exit_slot(device_id)
        time.sleep(3)
        final = self.screen_reader.read_balance(device_id, book)
        final_balance = final.data.raw_balance if final.success else initial_balance

        return self._build_result(book, collaboratore_id, promo_config, phases, initial_balance, final_balance)

    def _execute_phase(
        self,
        device_id: str,
        slot_name: str,
        target_amount: float,
        spin_amount: float,
        phase_name: str,
    ) -> CasinoPhaseResult:
        """Esegue una singola fase della promo (spin fino al target, gia'
        dentro la slot). La navigazione verso la slot resta a carico del
        chiamante (book-specific adapter, non ancora scritto)."""
        initial_balance = self._read_slot_balance(device_id)
        if initial_balance is None:
            return CasinoPhaseResult(
                phase=phase_name, slot_name=slot_name,
                initial_balance=0, final_balance=0,
                target_amount=target_amount, played_amount=0,
                spins_done=0, spins_needed=0, completed=False,
                notes=["Cannot read initial balance via OCR"],
            )

        spins_needed = self._compute_spins_needed(target_amount, spin_amount)
        spins_done = 0

        logger.info(
            "Phase %s: %d spins needed (target=%.2f, spin=%.2f)",
            phase_name, spins_needed, target_amount, spin_amount,
        )

        current_balance = initial_balance

        while not self._stop_requested and spins_done < spins_needed:
            remaining = spins_needed - spins_done

            spin_coords = self._find_spin_button(device_id) or (540, 1900)
            self.touch_injector.tap_coordinates(device_id, spin_coords[0], spin_coords[1])
            spins_done += 1
            time.sleep(1.5)  # aspetta che lo spin finisca

            if remaining <= 10:
                # Ultimi 10 spin (compresi gli ultimi 3): leggi dopo ogni spin
                read = self._read_slot_balance(device_id)
                if read is not None:
                    current_balance = read
                    if current_balance < spin_amount:
                        logger.info("Saldo insufficiente per un altro spin. Stop.")
                        break
            elif spins_done % 10 == 0:
                read = self._read_slot_balance(device_id)
                if read is not None:
                    current_balance = read
                played = initial_balance - current_balance
                logger.info(
                    "Spin %d/%d | Saldo: %.2f | Played: %.2f",
                    spins_done, spins_needed, current_balance, played,
                )
                if read is not None and current_balance < spin_amount:
                    logger.info("Saldo insufficiente. Stop.")
                    break

        final_balance = current_balance
        played = initial_balance - final_balance

        return CasinoPhaseResult(
            phase=phase_name, slot_name=slot_name,
            initial_balance=initial_balance, final_balance=final_balance,
            target_amount=target_amount, played_amount=played,
            spins_done=spins_done, spins_needed=spins_needed,
            completed=spins_done >= spins_needed,
        )

    def _execute_fun_phase(
        self,
        device_id: str,
        slot_name: str,
        target_balance: float,
        rollover_target: float,
        spin_high: float,
        spin_low: float,
        fun_bonus_amount: float,
    ) -> CasinoPhaseResult:
        """Fase 2: prima raggiungi target balance con spin alti, poi fai il
        rollover con spin minimi."""
        initial_balance = self._read_slot_balance(device_id)
        if initial_balance is None:
            initial_balance = fun_bonus_amount
        spins_done = 0

        # Sotto-fase A: raggiungi target balance con spin alti
        logger.info("Fase 2A: Target balance %.2f con spin %.2f", target_balance, spin_high)

        current = initial_balance
        while not self._stop_requested and current < target_balance:
            spin_coords = self._find_spin_button(device_id) or (540, 1900)
            self.touch_injector.tap_coordinates(device_id, spin_coords[0], spin_coords[1])
            spins_done += 1
            time.sleep(1.5)

            if spins_done % 5 == 0:
                read = self._read_slot_balance(device_id)
                current = read if read is not None else current
                logger.info("Fase 2A spin %d | Saldo: %.2f | Target: %.2f", spins_done, current, target_balance)

                if current < spin_high:
                    logger.info("Saldo insufficiente per spin alto. Passo a spin minimi.")
                    break

        if current >= target_balance:
            logger.info("Target balance raggiunto: %.2f", current)

        # Sotto-fase B: fai rollover con spin minimi
        rollover_spins_needed = self._compute_spins_needed(rollover_target, spin_low)
        rollover_spins_done = 0

        logger.info("Fase 2B: Rollover %.2f con spin %.2f (%d spin)", rollover_target, spin_low, rollover_spins_needed)

        while not self._stop_requested and rollover_spins_done < rollover_spins_needed:
            remaining = rollover_spins_needed - rollover_spins_done

            spin_coords = self._find_spin_button(device_id) or (540, 1900)
            self.touch_injector.tap_coordinates(device_id, spin_coords[0], spin_coords[1])
            rollover_spins_done += 1
            time.sleep(1.5)

            if remaining <= 10:
                read = self._read_slot_balance(device_id)
                if read is not None:
                    current = read
                    if current < spin_low:
                        logger.info("Saldo insufficiente. Stop rollover.")
                        break
            elif rollover_spins_done % 10 == 0:
                read = self._read_slot_balance(device_id)
                if read is not None:
                    current = read
                logger.info("Rollover spin %d/%d | Saldo: %.2f", rollover_spins_done, rollover_spins_needed, current)

        total_spins = spins_done + rollover_spins_done
        final_balance = current

        return CasinoPhaseResult(
            phase="fun", slot_name=slot_name,
            initial_balance=initial_balance, final_balance=final_balance,
            target_amount=rollover_target, played_amount=rollover_target,
            spins_done=total_spins, spins_needed=rollover_spins_needed + spins_done,
            completed=rollover_spins_done >= rollover_spins_needed,
        )

    def _compute_spins_needed(self, target_amount: float, spin_amount: float) -> int:
        """int(target/spin) tronca male in binario (es. 2.8/0.10 = 27.999999999999996
        -> 27 invece di 28): passa per Decimal(str(...)) per avere una divisione
        esatta sui valori decimali che maneggiamo (euro con 2 cifre)."""
        target = Decimal(str(target_amount))
        spin = Decimal(str(spin_amount))
        return int(target / spin)

    def _read_slot_balance(self, device_id: str) -> Optional[float]:
        """Legge il saldo dentro la slot via EasyOCR su screenshot."""
        ss = self.screen_reader._screenshot(device_id)
        if not ss.success:
            return None

        state = self.ocr_reader.read_slot_state(ss.data)
        return state.get("saldo")

    def _find_spin_button(self, device_id: str) -> Optional[tuple]:
        """Trova le coordinate del tasto Spin via OCR."""
        ss = self.screen_reader._screenshot(device_id)
        if not ss.success:
            return None

        state = self.ocr_reader.read_slot_state(ss.data)
        return state.get("spin_button_coords")

    def _exit_slot(self, device_id: str):
        """Esce dalla slot cliccando 'Esci'."""
        result = self.screen_reader.find_element(device_id, "Esci")
        if result.success:
            elem = result.data
            cx = (elem.bounds[0] + elem.bounds[2]) // 2
            cy = (elem.bounds[1] + elem.bounds[3]) // 2
            self.touch_injector.tap_coordinates(device_id, cx, cy)
            time.sleep(2)

    def _read_all_balances(self, device_id: str, book: str) -> dict:
        """Legge tutti i saldi separati dalla home page del book (real,
        real bonus, fun/wagering bonus, giocate aperte)."""
        result = self.screen_reader.read_balance(device_id, book)
        if result.success:
            return {
                "real_balance": result.data.raw_balance,
                "real_bonus": result.data.real_bonus,
                "wagering_bonus": result.data.wagering_bonus,
                "open_bets": result.data.open_bets,
            }
        return {}

    def _build_result(
        self, book, coll_id, promo_config, phases, initial_balance, final_balance=None,
    ) -> Result[CasinoResult]:
        if final_balance is None:
            final_balance = initial_balance
        total_profit = final_balance - initial_balance
        return Result.ok(CasinoResult(
            book=book, collaboratore_id=coll_id,
            promo_name=promo_config.get("promo_name", "unknown"),
            phases=phases, total_profit=total_profit,
            initial_balance=initial_balance, final_balance=final_balance,
            completed=all(p.completed for p in phases),
        ))

    def stop(self):
        """Ferma l'esecuzione (chiamato dall'esterno per HITL)."""
        self._stop_requested = True
