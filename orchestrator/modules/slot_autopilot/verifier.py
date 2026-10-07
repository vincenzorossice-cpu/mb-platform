import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import StabilityResult, diff_score, wait_until_stable

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


_PARTIAL_CALM_STREAK = 3   # confronti consecutivi con le zone ferme (~1 s)
# Dopo un tocco lo spin parte entro un istante: la freccia dello spin non puo' dirsi "tornata" prima di questo tempo.
_MIN_SECONDS_BEFORE_READY = 1.2
_READY_STREAK_AFTER_TAP = 2   # screenshot di fila con la freccia visibile per dire che lo spin e' finito


@dataclass
class CalmResult:
    """Esito di wait_until_calm: lo schermo e' fermo? E' stato necessario aspettare?"""
    screenshot_path: str
    calm: bool
    waited: bool
    seconds: float
    partial: bool = False   # fermi solo la barra degli importi e il pulsante spin, i rulli si muovono


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
    # Dove e' andato il tempo di questo tocco (secondi): il tocco (con la sua attesa), l'attesa dello schermo fermo
    # con il numero di scatti, la lettura OCR.
    tap_seconds: float = 0.0
    wait_seconds: float = 0.0
    wait_captures: int = 0
    read_seconds: float = 0.0
    # True se l'attesa e' finita perche' la freccia dello spin e' tornata visibile (gioco pronto).
    ended_ready: bool = False


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
        # Contatori per capire dove va il tempo (secondi e numero di eventi): vedi snapshot().
        self.stats = {"tap_s": 0.0, "taps": 0, "wait_s": 0.0, "captures": 0, "read_s": 0.0, "reads": 0}

    def snapshot(self) -> dict:
        return dict(self.stats)

    def verified_tap(
        self, device_id: str, x: int, y: int, expectation: Expectation,
        screenshot_before: Optional[str] = None, state_before: Optional[dict] = None,
        region_hint: Optional[tuple] = None, ready_check=None, on_after_tap=None, initial_wait: float = 0.0,
    ) -> Result[TapOutcome]:
        """`region_hint` (x1,y1,x2,y2): lettura OCR rapida "dopo" solo su quella fascia
        (saldo/puntata); se li' mancano saldo o puntata si rilegge lo stesso screenshot
        a schermo intero, cosi' una fascia sbagliata non genera falsi "nessun effetto".
        `ready_check(path) -> True | False | None`: se dato (e utilizzabile) l'attesa dopo il tocco finisce quando
        la freccia dello spin e' tornata visibile invece che a schermo fermo. `on_after_tap()`: chiamata subito
        dopo il tocco (serve ad apprendere come appare il pulsante durante l'animazione).
        `initial_wait`: secondi senza scattare dopo il tocco (l'animazione c'e' di sicuro)."""
        try:
            return self._verified_tap(
                device_id, x, y, expectation, screenshot_before, state_before, region_hint, ready_check, on_after_tap,
                initial_wait,
            )
        except Exception as exc:
            return Result.fail(f"Unexpected error during verified_tap: {exc}", "VERIFICATION_ERROR")

    def wait_until_calm(
        self, device_id: str, reference_screenshot: str, max_wait_seconds: Optional[float] = None,
        regions: Optional[list] = None,
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
        if regions:
            return self._wait_calm_with_regions(device_id, shot.data, started, max_wait_seconds, regions)
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

    def _wait_calm_with_regions(
        self, device_id: str, first_path: str, started: float, max_wait_seconds: Optional[float], regions: list,
    ) -> Result[CalmResult]:
        """Come wait_until_stable, ma accetta anche la 'calma parziale': l'intero schermo si muove (overlay di una
        vincita che compare e scompare) ma tutte le zone indicate restano ferme per _PARTIAL_CALM_STREAK
        confronti di fila. Se una zona cambia (vincita che si conta, spin in corso) non e' calma."""
        threshold = self.config.frame_diff_stable_threshold
        limit = max_wait_seconds or self.config.frame_diff_max_wait_seconds
        previous = first_path
        streak = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= limit:
                return Result.ok(CalmResult(previous, calm=False, waited=True, seconds=elapsed))
            time.sleep(self.config.frame_diff_poll_interval)
            shot = self.screen_reader._screenshot(device_id)
            if not shot.success:
                return Result.fail(shot.error, shot.error_code)
            whole = diff_score(previous, shot.data)
            if not whole.success:
                return Result.fail(whole.error, whole.error_code)
            if whole.data < threshold:
                return Result.ok(CalmResult(shot.data, calm=True, waited=True, seconds=time.monotonic() - started))
            zones_still = True
            for region in regions:
                part = diff_score(previous, shot.data, region)
                if not part.success or part.data >= threshold:
                    zones_still = False
                    break
            streak = streak + 1 if zones_still else 0
            if streak >= _PARTIAL_CALM_STREAK:
                return Result.ok(CalmResult(
                    shot.data, calm=True, waited=True, seconds=time.monotonic() - started, partial=True,
                ))
            previous = shot.data

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
        region_hint: Optional[tuple] = None, ready_check=None, on_after_tap=None, initial_wait: float = 0.0,
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
        tap_started = time.monotonic()
        tap_result = self.touch_injector.tap_coordinates(device_id, x, y)
        tap_seconds = time.monotonic() - tap_started
        if not tap_result.success:
            logger.warning("verified_tap: tap fallito: %s (%s)", tap_result.error, tap_result.error_code)
            return Result.fail(tap_result.error, tap_result.error_code)

        if on_after_tap is not None:
            try:
                on_after_tap()
            except Exception as exc:
                logger.warning("verified_tap: on_after_tap fallito: %s", exc)

        wait_started = time.monotonic()
        wait_result, ended_ready = self._wait_after_tap(
            device_id, expectation, ready_check, wait_started, initial_wait,
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
        self.stats["tap_s"] += tap_seconds
        self.stats["taps"] += 1
        self.stats["wait_s"] += wait_seconds
        self.stats["captures"] += wait_captures
        self.stats["read_s"] += read_seconds
        self.stats["reads"] += 1

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
            tap_seconds=tap_seconds,
            wait_seconds=wait_seconds,
            wait_captures=wait_captures,
            read_seconds=read_seconds,
            ended_ready=ended_ready,
        ))

    def _wait_after_tap(
        self, device_id: str, expectation: Expectation, ready_check, tap_moment: float, initial_wait: float = 0.0,
    ) -> tuple:
        """(Result[StabilityResult], ended_ready). Con ready_check utilizzabile l'attesa finisce al ritorno della
        freccia dello spin (2 screenshot di fila, non prima di _MIN_SECONDS_BEFORE_READY dal tocco); se la
        freccia non e' classificabile, o non torna entro il tetto, vale lo schermo fermo come sempre."""
        if initial_wait > 0:
            time.sleep(initial_wait)     # nei primi secondi l'animazione c'e' di sicuro: niente screenshot inutili
        if ready_check is not None:
            limit = max(self.config.frame_diff_max_wait_seconds, self.config.spin_calm_max_wait_seconds)
            streak = captures = 0
            seen_busy = False
            while True:
                shot = self.screen_reader._screenshot(device_id)
                if not shot.success:
                    return Result.fail(shot.error, shot.error_code), False
                captures += 1
                state = ready_check(shot.data)
                if state is None and captures == 1:
                    break                                   # non classificabile: ripiego sullo schermo fermo
                now = time.monotonic()
                if state is False:
                    seen_busy = True
                streak = streak + 1 if (state and now - tap_moment >= _MIN_SECONDS_BEFORE_READY) else 0
                # Una transizione occupato -> pronto vista dal vivo e' gia' la prova: basta un frame pronto.
                if streak >= (1 if seen_busy else _READY_STREAK_AFTER_TAP):
                    return Result.ok(StabilityResult(shot.data, stable=True, waited_seconds=now - tap_moment,
                                                     captures=captures)), True
                if now - tap_moment >= limit:
                    logger.info("verified_tap: la freccia dello spin non e' tornata entro %.0f s: schermo fermo.", limit)
                    break
                time.sleep(self.config.frame_diff_poll_interval)
        return wait_until_stable(
            lambda: self.screen_reader._screenshot(device_id),
            max_wait_seconds=self.config.frame_diff_max_wait_seconds,
            poll_interval=self.config.frame_diff_poll_interval,
            stable_threshold=self.config.frame_diff_stable_threshold,
            region=expectation.region,
        ), False

    def wait_until_button_ready(self, device_id: str, ready_check, max_wait_seconds: float) -> Optional[Result[CalmResult]]:
        """Prima del tocco: la freccia dello spin e' visibile? Un solo screenshot se lo e' gia'; se non lo e' si
        aspetta (2 screenshot di fila con la freccia). None se la freccia non e' classificabile (chi chiama
        usa il controllo di calma di sempre)."""
        started = time.monotonic()
        streak = captures = 0
        while True:
            shot = self.screen_reader._screenshot(device_id)
            if not shot.success:
                return Result.fail(shot.error, shot.error_code)
            state = ready_check(shot.data)
            if state is None and captures == 0:
                return None
            captures += 1
            streak = streak + 1 if state else 0
            if streak >= (1 if captures == 1 else _READY_STREAK_AFTER_TAP):
                return Result.ok(CalmResult(shot.data, calm=True, waited=captures > 1,
                                            seconds=time.monotonic() - started))
            if time.monotonic() - started >= max_wait_seconds:
                return Result.ok(CalmResult(shot.data, calm=False, waited=True, seconds=time.monotonic() - started))
            time.sleep(self.config.frame_diff_poll_interval)

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
