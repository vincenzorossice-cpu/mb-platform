from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import diff_score, wait_until_stable


def classify_screen_state(ocr_state: dict) -> str:
    """Proxy OCR-only, senza GPT: 'idle' se il bottone spin e' rilevato
    nell'ultima lettura OCR, altrimenti 'unsafe'. Semplificazione deliberata
    (2 soli valori, contro i 10 di screen_state via GPT Vision) - errare
    per eccesso di cautela piuttosto che rischiare un tap durante una
    transizione. Vedi punto aperto nel report finale."""
    return "idle" if ocr_state.get("spin_button_coords") is not None else "unsafe"


@dataclass
class Expectation:
    kind: str  # "balance_decrease" | "bet_decrease" | "bet_unchanged_confirms_min" | "screen_changed"
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

    def verified_tap(self, device_id: str, x: int, y: int, expectation: Expectation) -> Result[TapOutcome]:
        try:
            return self._verified_tap(device_id, x, y, expectation)
        except Exception as exc:
            return Result.fail(f"Unexpected error during verified_tap: {exc}", "VERIFICATION_ERROR")

    def _verified_tap(self, device_id: str, x: int, y: int, expectation: Expectation) -> Result[TapOutcome]:
        screenshot_before_result = self.screen_reader._screenshot(device_id)
        if not screenshot_before_result.success:
            return Result.fail(screenshot_before_result.error, screenshot_before_result.error_code)
        screenshot_before = screenshot_before_result.data

        try:
            state_before = self.ocr_reader.read_slot_state(screenshot_before)
        except Exception as exc:
            return Result.fail(f"OCR error before tap: {exc}", "VERIFICATION_ERROR")

        tap_result = self.touch_injector.tap_coordinates(device_id, x, y)
        if not tap_result.success:
            return Result.fail(tap_result.error, tap_result.error_code)

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

        try:
            state_after = self.ocr_reader.read_slot_state(screenshot_after)
        except Exception as exc:
            return Result.fail(f"OCR error after tap: {exc}", "VERIFICATION_ERROR")

        diff_result = diff_score(screenshot_before, screenshot_after, expectation.region)
        if not diff_result.success:
            return Result.fail(diff_result.error, diff_result.error_code)
        diff = diff_result.data

        balance_before = _to_decimal(state_before.get("saldo"))
        balance_after = _to_decimal(state_after.get("saldo"))
        bet_before = _to_decimal(state_before.get("posta"))
        bet_after = _to_decimal(state_after.get("posta"))

        verified = self._evaluate_expectation(
            expectation, diff, balance_before, balance_after, bet_before, bet_after,
        )
        outcome = _classify_outcome(verified, diff, state_before, state_after, self.config)

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
        ))

    def _evaluate_expectation(self, expectation: Expectation, diff: float,
                               balance_before, balance_after, bet_before, bet_after) -> bool:
        if expectation.kind == "balance_decrease":
            if balance_before is None or balance_after is None:
                return False
            return (balance_before - balance_after) >= expectation.min_decrease
        if expectation.kind == "bet_decrease":
            if bet_before is None or bet_after is None:
                return False
            return bet_after < bet_before
        if expectation.kind == "bet_unchanged_confirms_min":
            if bet_before is None or bet_after is None:
                return False
            return bet_before == bet_after and diff < self.config.frame_diff_stable_threshold
        if expectation.kind == "screen_changed":
            return diff >= self.config.frame_diff_stable_threshold
        return False
