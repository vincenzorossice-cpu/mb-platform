import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import diff_score, wait_until_stable

logger = logging.getLogger(__name__)


def classify_screen_state(ocr_state: dict) -> str:
    """Proxy OCR-only, senza GPT: 'idle' se il bottone spin e' rilevato
    nell'ultima lettura OCR, altrimenti 'unsafe'. Semplificazione deliberata
    (2 soli valori, contro i 10 di screen_state via GPT Vision) - errare
    per eccesso di cautela piuttosto che rischiare un tap durante una
    transizione. Vedi punto aperto nel report finale.

    Fallback: molte slot (es. Pirots5) hanno un bottone spin puramente
    iconografico, senza mai il testo "SPIN"/"GIRA" leggibile via OCR - senza
    questo fallback classify_screen_state ritornerebbe sempre 'unsafe' per
    quelle slot, bloccando ogni spin per sempre. Se saldo e puntata sono
    entrambi leggibili puliti, e' un segnale ragionevole (anche se non
    preciso quanto un giudizio visivo) che lo schermo e' fermo: durante un
    caricamento, uno spin in corso o una vincita questi numeri sono
    tipicamente in transizione o coperti da animazioni."""
    if ocr_state.get("spin_button_coords") is not None:
        return "idle"
    if ocr_state.get("saldo") is not None and ocr_state.get("posta") is not None:
        return "idle"
    return "unsafe"


@dataclass
class CalmResult:
    """Esito di wait_until_calm: lo schermo e' fermo? E' stato necessario aspettare?"""
    screenshot_path: str
    calm: bool
    waited: bool
    seconds: float


@dataclass
class Expectation:
    kind: str  # "balance_decrease" | "balance_changed" | "bet_decrease" | "bet_increase" | "bet_unchanged_confirms_min" | "screen_changed"
    min_decrease: Decimal = Decimal("0.00")
    region: Optional[tuple] = None


@dataclass
class TapOutcome:
    tapped_coords: tuple  # la coordinata RICHIESTA, mai quella con jitter di TouchInjector
    verified: bool
    screenshot_before: str
    screenshot_after: Optional[str]
    diff_score: Optional[float]
    balance_before: Optional[Decimal]
    balance_after: Optional[Decimal]
    bet_before: Optional[Decimal]
    bet_after: Optional[Decimal]
    screen_state_before: str
    screen_state_after: Optional[str]
    outcome: str  # "success" | "no_effect" | "unexpected_change" | "error"
    error_code: Optional[str] = None
    # Letta dallo stesso state_after gia' usato per verificare il tap: zero
    # chiamate OCR aggiuntive. Puo' essere None anche dopo una vincita reale
    # se l'OCR non legge l'etichetta "VINCITA" in quel preciso momento.
    win_amount: Optional[Decimal] = None
    # La lettura OCR completa dopo il tocco (testi inclusi): chi ha bisogno dello
    # stato dello schermo subito dopo puo' riusarla invece di rifare l'OCR.
    state_after: Optional[dict] = None


def _to_decimal(value) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


def _classify_outcome(verified: bool, diff: float, state_before: dict, state_after: dict,
                       config: AutoPilotConfig) -> str:
    if verified:
        return "success"
    unchanged = (
        state_before.get("saldo") == state_after.get("saldo")
        and state_before.get("posta") == state_after.get("posta")
    )
    if diff < config.frame_diff_stable_threshold and unchanged:
        return "no_effect"
    return "unexpected_change"


class ActionVerifier:
    """Ogni tap del sistema passa da qui: screenshot prima, tap, attesa di
    stabilizzazione, screenshot dopo, confronto contro l'aspettativa
    dichiarata. Nessuna eccezione esce da verified_tap."""

    def __init__(self, touch_injector, screen_reader, ocr_reader, config: AutoPilotConfig):
        self.touch_injector = touch_injector
        self.screen_reader = screen_reader
        self.ocr_reader = ocr_reader
        self.config = config

    def verified_tap(
        self, device_id: str, x: int, y: int, expectation: Expectation,
        screenshot_before: Optional[str] = None, state_before: Optional[dict] = None,
        region_hint: Optional[tuple] = None,
    ) -> Result[TapOutcome]:
        """`region_hint` (x1,y1,x2,y2): lettura OCR rapida "dopo" solo su quella fascia
        (saldo/puntata); se li' mancano saldo o puntata si rilegge lo stesso screenshot
        a schermo intero, cosi' una fascia sbagliata non genera falsi "nessun effetto"."""
        try:
            return self._verified_tap(
                device_id, x, y, expectation, screenshot_before, state_before, region_hint,
            )
        except Exception as exc:
            return Result.fail(f"Unexpected error during verified_tap: {exc}", "VERIFICATION_ERROR")

    def wait_until_calm(
        self, device_id: str, reference_screenshot: str, max_wait_seconds: Optional[float] = None,
    ) -> Result[CalmResult]:
        """Lo schermo e' fermo adesso? UNO screenshot confrontato in locale (nessuna AI, nessun token) con
        l'ultimo schermo noto: uguale -> si puo' toccare subito; diverso -> si aspetta che si fermi, fino a
        `max_wait_seconds`. Serve a non toccare dentro un'animazione (vincita, conteggio del saldo)."""
        started = time.monotonic()
        shot = self.screen_reader._screenshot(device_id)
        if not shot.success:
            return Result.fail(shot.error, shot.error_code)
        diff = diff_score(reference_screenshot, shot.data)
        if diff.success and diff.data < self.config.frame_diff_stable_threshold:
            return Result.ok(CalmResult(shot.data, calm=True, waited=False, seconds=time.monotonic() - started))
        wait = wait_until_stable(
            lambda: self.screen_reader._screenshot(device_id),
            max_wait_seconds=max_wait_seconds or self.config.frame_diff_max_wait_seconds,
            poll_interval=self.config.frame_diff_poll_interval,
            stable_threshold=self.config.frame_diff_stable_threshold,
        )
        if not wait.success:
            return Result.fail(wait.error, wait.error_code)
        return Result.ok(CalmResult(
            wait.data.screenshot_path, calm=wait.data.stable, waited=True, seconds=time.monotonic() - started,
        ))

    def _read_state_after(self, screenshot_path: str, region_hint: Optional[tuple]) -> dict:
        if region_hint is None or not hasattr(self.ocr_reader, "set_region"):
            return self.ocr_reader.read_slot_state(screenshot_path)
        original = getattr(self.ocr_reader, "region", None)
        self.ocr_reader.set_region(region_hint)
        try:
            state = self.ocr_reader.read_slot_state(screenshot_path)
            if state.get("saldo") is None or state.get("posta") is None:
                self.ocr_reader.set_region(original)
                state = self.ocr_reader.read_slot_state(screenshot_path)
            return state
        finally:
            self.ocr_reader.set_region(original)

    def _verified_tap(
        self, device_id: str, x: int, y: int, expectation: Expectation,
        screenshot_before: Optional[str] = None, state_before: Optional[dict] = None,
        region_hint: Optional[tuple] = None,
    ) -> Result[TapOutcome]:
        # Se il chiamante ha gia' una lettura fresca dello STESSO schermo (presa
        # un istante prima, senza nulla in mezzo che lo cambi), riusarla evita
        # una seconda, costosa passata OCR quasi identica alla prima.
        if screenshot_before is None or state_before is None:
            screenshot_before_result = self.screen_reader._screenshot(device_id)
            if not screenshot_before_result.success:
                return Result.fail(screenshot_before_result.error, screenshot_before_result.error_code)
            screenshot_before = screenshot_before_result.data

            try:
                state_before = self.ocr_reader.read_slot_state(screenshot_before)
            except Exception as exc:
                return Result.fail(f"OCR error before tap: {exc}", "VERIFICATION_ERROR")

        logger.info("verified_tap: tap (%d,%d), attesa '%s'", x, y, expectation.kind)
        tap_result = self.touch_injector.tap_coordinates(device_id, x, y)
        if not tap_result.success:
            logger.warning("verified_tap: tap fallito: %s (%s)", tap_result.error, tap_result.error_code)
            return Result.fail(tap_result.error, tap_result.error_code)

        wait_started = time.monotonic()
        wait_result = wait_until_stable(
            lambda: self.screen_reader._screenshot(device_id),
            max_wait_seconds=self.config.frame_diff_max_wait_seconds,
            poll_interval=self.config.frame_diff_poll_interval,
            stable_threshold=self.config.frame_diff_stable_threshold,
            region=expectation.region,
        )
        if not wait_result.success:
            return Result.fail(wait_result.error, wait_result.error_code)
        screenshot_after = wait_result.data.screenshot_path

        wait_seconds = time.monotonic() - wait_started
        wait_captures = wait_result.data.captures
        read_started = time.monotonic()
        try:
            state_after = self._read_state_after(screenshot_after, region_hint)
        except Exception as exc:
            return Result.fail(f"OCR error after tap: {exc}", "VERIFICATION_ERROR")
        read_seconds = time.monotonic() - read_started

        diff_result = diff_score(screenshot_before, screenshot_after, expectation.region)
        if not diff_result.success:
            return Result.fail(diff_result.error, diff_result.error_code)
        diff = diff_result.data

        balance_before = _to_decimal(state_before.get("saldo"))
        balance_after = _to_decimal(state_after.get("saldo"))
        bet_before = _to_decimal(state_before.get("posta"))
        bet_after = _to_decimal(state_after.get("posta"))
        win_amount = _to_decimal(state_after.get("vincita"))

        verified = self._evaluate_expectation(
            expectation, diff, balance_before, balance_after, bet_before, bet_after,
        )
        outcome = _classify_outcome(verified, diff, state_before, state_after, self.config)
        logger.info(
            "verified_tap: esito=%s verified=%s diff=%.3f (bet %s->%s, saldo %s->%s) "
            "[attesa schermo fermo %.1f s in %d scatti, lettura %.1f s]",
            outcome, verified, diff, bet_before, bet_after, balance_before, balance_after, wait_seconds,
            wait_captures, read_seconds,
        )

        return Result.ok(TapOutcome(
            tapped_coords=(x, y),
            verified=verified,
            screenshot_before=screenshot_before,
            screenshot_after=screenshot_after,
            diff_score=diff,
            balance_before=balance_before,
            balance_after=balance_after,
            bet_before=bet_before,
            bet_after=bet_after,
            screen_state_before=classify_screen_state(state_before),
            screen_state_after=classify_screen_state(state_after),
            outcome=outcome,
            win_amount=win_amount,
            state_after=state_after,
        ))

    def _evaluate_expectation(self, expectation: Expectation, diff: float,
                               balance_before, balance_after, bet_before, bet_after) -> bool:
        if expectation.kind == "balance_decrease":
            if balance_before is None or balance_after is None:
                return False
            return (balance_before - balance_after) >= expectation.min_decrease
        if expectation.kind == "balance_changed":
            # Usata per verificare lo spin: una vincita puo' far RIMANERE
            # UGUALE o SALIRE il saldo invece di scendere della puntata -
            # richiedere specificamente un calo (come "balance_decrease")
            # classificherebbe una vincita come spin fallito, causando
            # ritap inutili (e rischiosi) sullo stesso spin gia' riuscito.
            if balance_before is None or balance_after is None:
                return False
            return balance_after != balance_before
        if expectation.kind == "bet_decrease":
            if bet_before is None or bet_after is None:
                return False
            return bet_after < bet_before
        if expectation.kind == "bet_increase":
            if bet_before is None or bet_after is None:
                return False
            return bet_after > bet_before
        if expectation.kind == "bet_unchanged_confirms_min":
            if bet_before is None or bet_after is None:
                return False
            return bet_before == bet_after and diff < self.config.frame_diff_stable_threshold
        if expectation.kind == "screen_changed":
            return diff >= self.config.frame_diff_stable_threshold
        return False
