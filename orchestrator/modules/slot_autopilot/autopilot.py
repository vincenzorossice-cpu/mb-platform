from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.screen_reader.interface import ScreenReaderInterface
from orchestrator.modules.slot_autopilot.bet_controller import BetController, BetResult
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.spin_controller import SpinController, SpinResult
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_ocr.reader import SlotOCRReader
from orchestrator.modules.step_logger.interface import EpisodeSummary, StepLoggerInterface
from orchestrator.modules.touch_injector.interface import TouchInjectorInterface


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
        step_logger: StepLoggerInterface,
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

        self._profile = None
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

    def prepare(self, device_id: str) -> Result[bool]:
        try:
            profile_result = self.profile_loader.load(self.config.slot_name)
            if profile_result.success:
                self._profile = profile_result.data
            elif self.config.require_profile:
                return Result.fail(profile_result.error, profile_result.error_code)
            else:
                self._profile = None

            episode_result = self.step_logger.start_episode(self.config.slot_name, device_id)
            if not episode_result.success:
                return Result.fail(episode_result.error, episode_result.error_code)
            self._episode_id = episode_result.data

            context_result = self._build_context(device_id)
            if context_result.success:
                value = context_result.data.slot_ocr_state.get("saldo")
                self._initial_balance = Decimal(str(value)) if value is not None else None
                self._last_known_balance = self._initial_balance

            return Result.ok(True)
        except Exception as exc:
            return Result.fail(f"Unexpected error in prepare: {exc}", "UNEXPECTED_ERROR")

    def set_min_bet(self, device_id: str) -> Result[BetResult]:
        try:
            result = self.bet_controller.set_min_bet(
                device_id, self.config.slot_name, self._episode_id,
                lambda: self._build_context(device_id),
            )
            if result.success:
                self._total_steps += result.data.attempts
                self._min_bet_verified = result.data.min_bet_verified
                self._current_bet = result.data.final_bet
            return result
        except Exception as exc:
            return Result.fail(f"Unexpected error in set_min_bet: {exc}", "UNEXPECTED_ERROR")

    def spin(self, device_id: str, spin_index: int) -> Result[SpinResult]:
        try:
            result = self.spin_controller.spin(
                device_id, self.config.slot_name, self._episode_id, spin_index,
                lambda: self._build_context(device_id), self._min_bet_verified, self._current_bet,
            )
            if result.success:
                self._total_steps += 1
                if result.data.outcome == "success":
                    self._total_spins += 1
                    if result.data.bet_amount is not None:
                        self._total_played += result.data.bet_amount
                    if result.data.balance_after is not None:
                        self._last_known_balance = result.data.balance_after
            return result
        except Exception as exc:
            return Result.fail(f"Unexpected error in spin: {exc}", "UNEXPECTED_ERROR")

    def get_balance_summary(self) -> tuple:
        """(saldo_iniziale, ultimo_saldo_osservato) - per un riepilogo netto
        vinto/perso affidabile a fine sessione, senza dover sommare le
        vincite lette (inaffidabili) ad ogni singolo spin."""
        return self._initial_balance, self._last_known_balance

    def shutdown(self, device_id: str) -> Result[bool]:
        try:
            if self._episode_id is None:
                return Result.ok(True)
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

        return Result.ok(LocateContext(
            device_id=device_id, slot_name=self.config.slot_name, screenshot_path=screenshot_path,
            screen_width=width, screen_height=height,
            raw_texts=slot_ocr_state.get("raw_texts", []), text_boxes=text_boxes,
            slot_ocr_state=slot_ocr_state, profile=self._profile,
        ))
