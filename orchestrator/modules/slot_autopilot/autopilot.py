import dataclasses
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile_builder import ProfileBuilder, SessionEvidence
from orchestrator.modules.screen_reader.interface import ScreenReaderInterface
from orchestrator.modules.slot_autopilot.bet_controller import BetController, BetResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_ocr.reader import SlotOCRReader
from orchestrator.modules.step_logger.interface import EpisodeSummary, StepLoggerInterface
from orchestrator.modules.touch_injector.interface import TouchInjectorInterface


logger = logging.getLogger(__name__)

# Strategie che non implicano alcun aiuto esterno: dati gia' curati/verificati.
_UNASSISTED_STRATEGIES = {"profile", "memory", "menu"}
# Solo coordinate gia' curate/verificate: il loro spin puo' riusare lo schermo letto
# dopo il tocco come contesto dello spin successivo.
_REUSE_CONTEXT_STRATEGIES = {"profile", "memory"}

_AMOUNT_LABELS = SlotOCRReader._SALDO_LABELS + SlotOCRReader._POSTA_LABELS
# Il contesto letto da prepare() vale come lettura iniziale di set_min_bet se e' piu'
# recente di cosi' (stesso schermo, nessun tocco in mezzo).
_PREPARED_CONTEXT_MAX_AGE_SECONDS = 30
_BAND_MARGIN_PX = 40
_BAND_ROW_TOLERANCE_PX = 50
_BAND_MAX_CANVAS_FRACTION = 0.35


def learn_amounts_band(text_boxes: list, state: dict, canvas_bbox: Optional[tuple], screen_size: tuple) -> Optional[tuple]:
    """La fascia orizzontale dello schermo dove si leggono saldo e puntata (etichette
    e importi sulle stesse righe), +-margine, larghezza piena, dentro la canvas.
    Calcolata dallo schermo, nessun dato per slot. None se saldo/puntata non si
    leggono, se non ci sono etichette o se la fascia sarebbe troppo alta (etichette
    sparse: meglio leggere tutto)."""
    if state.get("saldo") is None or state.get("posta") is None:
        return None
    label_boxes = [b for b in text_boxes if any(word in b.text.upper() for word in _AMOUNT_LABELS)]
    if not label_boxes:
        return None
    rows = [(b.bbox[1] + b.bbox[3]) / 2 for b in label_boxes]
    value_boxes = [
        b for b in text_boxes
        if any(ch.isdigit() for ch in b.text)
        and any(abs((b.bbox[1] + b.bbox[3]) / 2 - row) < _BAND_ROW_TOLERANCE_PX for row in rows)
    ]
    boxes = label_boxes + value_boxes
    width, height = screen_size
    cx1, cy1, cx2, cy2 = canvas_bbox or (0, 0, width, height)
    y1 = max(cy1, min(b.bbox[1] for b in boxes) - _BAND_MARGIN_PX)
    y2 = min(cy2, max(b.bbox[3] for b in boxes) + _BAND_MARGIN_PX)
    if y2 <= y1 or (y2 - y1) > _BAND_MAX_CANVAS_FRACTION * (cy2 - cy1):
        return None
    return (cx1, int(y1), cx2, int(y2))


def _has_amounts(state: Optional[dict]) -> bool:
    return bool(state) and state.get("saldo") is not None and state.get("posta") is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlotAutoPilot:
    """Facciata: compone locator/bet_controller/spin_controller/step_logger
    gia' assemblati (nessuna istanziazione concreta qui, tutto iniettato) e
    offre un'API a 4 passi: prepare -> set_min_bet -> spin(n) -> shutdown."""

    def __init__(
        self, config: AutoPilotConfig, screen_reader: ScreenReaderInterface,
        touch_injector: TouchInjectorInterface, ocr_reader: SlotOCRReader,
        profile_loader: ProviderProfileLoader, locator: ElementLocator,
        bet_controller: BetController, spin_controller: SpinController,
        step_logger: StepLoggerInterface, profile_builder: Optional[ProfileBuilder] = None,
    ):
        self.config = config
        self.screen_reader = screen_reader
        self.touch_injector = touch_injector
        self.ocr_reader = ocr_reader
        self.profile_loader = profile_loader
        self.locator = locator
        self.bet_controller = bet_controller
        self.spin_controller = spin_controller
        self.step_logger = step_logger
        self.profile_builder = profile_builder

        self._profile = None
        self._screen_size: Optional[tuple] = None
        self._assisted = False
        self._no_effect_spins = 0
        self._episode_id: Optional[str] = None
        self._min_bet_verified = False
        self._current_bet: Optional[Decimal] = None
        self._total_spins = 0
        self._total_steps = 0
        self._total_played = Decimal("0.00")
        # Riepilogo finale affidabile (saldo iniziale vs ultimo osservato),
        # invece di sommare le vincite lette ad ogni spin (inaffidabili via
        # OCR - vedi Fix 11): _last_known_balance si aggiorna gratis da
        # SpinResult.balance_after, nessuna lettura extra.
        self._initial_balance: Optional[Decimal] = None
        self._last_known_balance: Optional[Decimal] = None
        # Velocita' del ciclo di spin: fascia di saldo/puntata appresa dallo schermo
        # e contesto riusabile (schermo stabile dopo l'ultimo spin verificato).
        self._amounts_band: Optional[tuple] = None
        self._cached_spin_context: Optional[LocateContext] = None
        self._spins_since_full_read = 0
        self._prepared_context: Optional[LocateContext] = None
        self._prepared_at = 0.0
        self._phase_marker: dict = {}

    def prepare(self, device_id: str) -> Result[bool]:
        try:
            profile_result = self.profile_loader.load(self.config.slot_name)
            if profile_result.success:
                self._profile = profile_result.data
            elif self.config.require_profile:
                return Result.fail(profile_result.error, profile_result.error_code)
            else:
                self._profile = None

            # La barra del browser non contiene mai testo di gioco: ignorarla
            # nell'OCR e' un risparmio sicuro (canvas_bbox e' un dato curato).
            if self._profile is not None and self._profile.verified and self._profile.canvas_bbox is not None:
                self.ocr_reader.set_region(self._profile.canvas_bbox)

            episode_result = self.step_logger.start_episode(self.config.slot_name, device_id)
            if not episode_result.success:
                return Result.fail(episode_result.error, episode_result.error_code)
            self._episode_id = episode_result.data

            context_result = self._build_context(device_id)
            if context_result.success:
                value = context_result.data.slot_ocr_state.get("saldo")
                self._initial_balance = Decimal(str(value)) if value is not None else None
                self._last_known_balance = self._initial_balance
                self._learn_band(context_result.data)
                context_result.data.amounts_band = self._amounts_band
                self._prepared_context = context_result.data
                self._prepared_at = time.monotonic()

            return Result.ok(True)
        except Exception as exc:
            return Result.fail(f"Unexpected error in prepare: {exc}", "UNEXPECTED_ERROR")

    def set_min_bet(self, device_id: str) -> Result[BetResult]:
        try:
            result = self.bet_controller.set_min_bet(
                device_id, self.config.slot_name, self._episode_id,
                lambda: self._bet_context(device_id),
            )
            if result.success:
                self._total_steps += result.data.attempts
                self._min_bet_verified = result.data.min_bet_verified
                self._current_bet = result.data.final_bet
                if any(name not in _UNASSISTED_STRATEGIES for name in result.data.strategy_sequence):
                    self._assisted = True
            return result
        except Exception as exc:
            return Result.fail(f"Unexpected error in set_min_bet: {exc}", "UNEXPECTED_ERROR")

    def phase_report(self, label: str, seconds: float) -> str:
        """Dove e' andato il tempo di una fase (preparazione, puntata minima): letture OCR (locali/remote), attese
        dello schermo fermo, tocchi. Differenza dai contatori dall'ultima chiamata; stringa vuota se non disponibili."""
        try:
            ocr_stats = self.ocr_reader.stats
            verifier = getattr(self.bet_controller, "verifier", None)
            current = {
                "ocr_calls": int(ocr_stats["calls"]), "ocr_s": float(ocr_stats["seconds"]),
                "ocr_remote": int(ocr_stats["remote_calls"]),
                **(verifier.snapshot() if verifier is not None else {}),
            }
            before = self._phase_marker
            self._phase_marker = current

            def delta(key):
                return current.get(key, 0) - before.get(key, 0)

            return (
                f"{label}: {seconds:.0f} s = letture OCR {delta('ocr_calls')} ({delta('ocr_s'):.1f} s, di cui "
                f"{delta('ocr_remote')} dal server remoto) + attese schermo fermo {delta('wait_s'):.1f} s in "
                f"{delta('captures')} screenshot + tocchi {delta('taps')} ({delta('tap_s'):.1f} s)"
            )
        except Exception:
            return ""

    def diagnose_busy(self, device_id: str) -> str:
        """Dopo un'attesa lunga di 'schermo occupato': Sol guarda lo schermo ('ready' | 'closed' | 'busy' |
        'unknown'). Senza supervisore (GPT spento) e' sempre 'unknown'."""
        supervisor = getattr(self, "supervisor", None)
        if supervisor is None:
            return "unknown"
        try:
            return supervisor.check_ready(device_id)
        except Exception as exc:   # il supervisore non deve mai rompere la sessione
            logger.warning("supervisore: controllo dello schermo occupato fallito: %s", exc)
            return "unknown"

    def save_debug_screenshot(self, device_id: str, label: str) -> Optional[str]:
        """Copia lo screenshot corrente in logs/<label>_<ora>.png per capire cosa blocca; None se non riesce."""
        try:
            import os
            import shutil
            shot = self.screen_reader._screenshot(device_id)
            if not shot.success:
                return None
            os.makedirs("logs", exist_ok=True)
            target = os.path.join("logs", f"{label}_{time.strftime('%Y%m%d_%H%M%S')}.png")
            shutil.copyfile(shot.data, target)
            return target
        except Exception as exc:
            logger.debug("screenshot di diagnosi non salvato: %s", exc)
            return None

    def spin(self, device_id: str, spin_index: int, force: bool = False) -> Result[SpinResult]:
        full_region = self.ocr_reader.region
        band = self._amounts_band
        spin_mode = getattr(self.ocr_reader, "set_spin_mode", None)
        try:
            if callable(spin_mode):
                spin_mode(True)
            if band is not None:
                # Lettura rapida: EasyOCR su una striscia di saldo/puntata invece
                # che su tutto lo schermo (~1 s invece di ~9 s).
                self.ocr_reader.set_region(band)
            result = self.spin_controller.spin(
                device_id, self.config.slot_name, self._episode_id, spin_index,
                lambda: self._spin_context(device_id, full_region), self._min_bet_verified, self._current_bet,
                **({"force": True} if force else {}),
            )
            self._spins_since_full_read += 1
            self._cached_spin_context = None
            if result.success:
                self._total_steps += 1
                if result.data.strategy_name is not None and result.data.strategy_name not in _UNASSISTED_STRATEGIES:
                    self._assisted = True
                if result.data.outcome in ("no_effect", "error"):
                    self._no_effect_spins += 1
                if result.data.outcome == "success":
                    self._total_spins += 1
                    if result.data.bet_amount is not None:
                        self._total_played += result.data.bet_amount
                    if result.data.balance_after is not None:
                        self._last_known_balance = result.data.balance_after
                    self._cached_spin_context = self._context_from_spin(result.data)
            return result
        except Exception as exc:
            return Result.fail(f"Unexpected error in spin: {exc}", "UNEXPECTED_ERROR")
        finally:
            if callable(spin_mode):
                spin_mode(False)
            if band is not None:
                self.ocr_reader.set_region(full_region)

    def _bet_context(self, device_id: str) -> Result[LocateContext]:
        """La prima lettura di set_min_bet e' quella appena fatta da prepare() (stesso
        schermo, pochi secondi fa): si riusa una volta sola invece di rifare ~9 s di OCR."""
        prepared, self._prepared_context = self._prepared_context, None
        if prepared is not None and time.monotonic() - self._prepared_at < _PREPARED_CONTEXT_MAX_AGE_SECONDS:
            return Result.ok(dataclasses.replace(prepared, device_id=device_id))
        return self._build_context(device_id)

    def _learn_band(self, context: LocateContext) -> None:
        try:
            canvas = self._profile.canvas_bbox if self._profile is not None and self._profile.verified else None
            self._amounts_band = learn_amounts_band(
                context.text_boxes, context.slot_ocr_state, canvas, (context.screen_width, context.screen_height),
            )
        except Exception as exc:
            logger.warning("Fascia di saldo/puntata non appresa: %s", exc)
            self._amounts_band = None
        if self._amounts_band is not None:
            logger.info("Fascia di saldo/puntata appresa: %s (lettura rapida durante gli spin)", self._amounts_band)

    def _context_from_spin(self, spin_result: SpinResult) -> Optional[LocateContext]:
        """Lo schermo stabile dopo uno spin verificato da una coordinata fidata e'
        gia' stato letto per verificarlo: e' il contesto dello spin successivo."""
        if (
            spin_result.strategy_name not in _REUSE_CONTEXT_STRATEGIES or spin_result.state_after is None
            or not spin_result.screenshot_after or self._screen_size is None
        ):
            return None
        return LocateContext(
            device_id="", slot_name=self.config.slot_name, screenshot_path=spin_result.screenshot_after,
            screen_width=self._screen_size[0], screen_height=self._screen_size[1],
            raw_texts=spin_result.state_after.get("raw_texts", []), text_boxes=[],
            slot_ocr_state=spin_result.state_after, profile=self._profile, amounts_band=self._amounts_band,
        )

    def _spin_context(self, device_id: str, full_region) -> Result[LocateContext]:
        """Contesto di inizio spin: quello gia' letto dopo lo spin precedente se c'e' (e
        mostra saldo e puntata); ogni N spin, o se la lettura rapida non trova saldo e
        puntata, UNA lettura completa prima di giudicare lo schermo."""
        cached, self._cached_spin_context = self._cached_spin_context, None
        force_full = self._amounts_band is not None and self._spins_since_full_read >= self.config.full_read_every_spins
        if cached is not None and not force_full and _has_amounts(cached.slot_ocr_state):
            return Result.ok(dataclasses.replace(cached, device_id=device_id))
        if self._amounts_band is None:
            return self._build_context(device_id)
        if force_full:
            return self._full_context(device_id, full_region)
        result = self._build_context(device_id)
        if result.success and not _has_amounts(result.data.slot_ocr_state):
            return self._full_context(device_id, full_region)
        return result

    def _full_context(self, device_id: str, full_region) -> Result[LocateContext]:
        self._spins_since_full_read = 0
        band = self.ocr_reader.region
        self.ocr_reader.set_region(full_region)
        try:
            return self._build_context(device_id)
        finally:
            self.ocr_reader.set_region(band)

    def get_balance_summary(self) -> tuple:
        """(saldo_iniziale, ultimo_saldo_osservato) - per un riepilogo netto
        vinto/perso affidabile a fine sessione, senza dover sommare le
        vincite lette (inaffidabili) ad ogni singolo spin."""
        return self._initial_balance, self._last_known_balance

    def _record_profile_evidence(self) -> None:
        """Bozza/promozione del profilo dalla sessione appena conclusa. Solo se
        la slot non ha un profilo curato; mai bloccante per lo shutdown."""
        if self.profile_builder is None or self._screen_size is None:
            return
        if self._profile is not None and not (self._profile.auto_generated and not self._profile.verified):
            return
        try:
            result = self.profile_builder.record_session(SessionEvidence(
                slot_name=self.config.slot_name,
                screen_width=self._screen_size[0], screen_height=self._screen_size[1],
                min_bet_verified=self._min_bet_verified, final_bet=self._current_bet,
                spins_ok=self._total_spins, assisted=self._assisted,
                no_effect_spins=self._no_effect_spins,
            ))
            if not result.success:
                logger.warning("Profilo auto-generato non aggiornato: %s (%s)", result.error, result.error_code)
        except Exception as exc:
            logger.warning("Profilo auto-generato non aggiornato: %s", exc)

    def shutdown(self, device_id: str) -> Result[bool]:
        try:
            if self._episode_id is None:
                return Result.ok(True)
            self._record_profile_evidence()
            summary = EpisodeSummary(
                episode_id=self._episode_id, slot_name=self.config.slot_name, device_id=device_id,
                started_at=_now(), ended_at=_now(), total_steps=self._total_steps,
                total_spins=self._total_spins, total_played=str(self._total_played),
                final_outcome="completed",
            )
            result = self.step_logger.end_episode(self._episode_id, summary)
            self._episode_id = None
            if not result.success and result.error_code == "EPISODE_NOT_STARTED":
                return Result.ok(True)
            return result
        except Exception as exc:
            return Result.fail(f"Unexpected error in shutdown: {exc}", "UNEXPECTED_ERROR")

    def _build_context(self, device_id: str) -> Result[LocateContext]:
        screenshot_result = self.screen_reader._screenshot(device_id)
        if not screenshot_result.success:
            return Result.fail(screenshot_result.error, screenshot_result.error_code)
        screenshot_path = screenshot_result.data

        try:
            slot_ocr_state = self.ocr_reader.read_slot_state(screenshot_path)
        except Exception as exc:
            return Result.fail(f"OCR error building context: {exc}", "CONTEXT_BUILD_ERROR")

        text_boxes_result = self.ocr_reader.read_text_boxes(screenshot_path)
        text_boxes = text_boxes_result.data if text_boxes_result.success else []

        try:
            from PIL import Image
            with Image.open(screenshot_path) as img:
                width, height = img.size
        except OSError as exc:
            return Result.fail(f"Cannot read screenshot dimensions: {exc}", "CONTEXT_BUILD_ERROR")

        self._screen_size = (width, height)
        return Result.ok(LocateContext(
            device_id=device_id, slot_name=self.config.slot_name, screenshot_path=screenshot_path,
            screen_width=width, screen_height=height,
            raw_texts=slot_ocr_state.get("raw_texts", []), text_boxes=text_boxes,
            slot_ocr_state=slot_ocr_state, profile=self._profile, amounts_band=self._amounts_band,
        ))
