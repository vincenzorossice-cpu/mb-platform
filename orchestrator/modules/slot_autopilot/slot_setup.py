"""Preparazione di una slot prima di giocare: la mappa salvata c'e' -> si usa (zero GPT); non c'e' -> la esplora Sol, la
si salva e la si usa. Il travaso nella memoria che il motore gia' consulta e' in SlotMemory.apply_to_memory."""
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.slot_explorer import SlotExplorer
from orchestrator.modules.slot_autopilot.slot_map import SlotMap, SlotMemory

logger = logging.getLogger(__name__)

USED = "USATA"
EXPLORED = "ESPLORATA"
NOT_MAPPED = "NON MAPPATA"


@dataclass
class SetupOutcome:
    status: str                           # USATA | ESPLORATA | NON MAPPATA
    gpt_calls: int = 0                    # chiamate GPT fatte dalla preparazione
    slot_map: Optional[SlotMap] = None


class SlotSetup:
    def __init__(self, memory: SlotMemory, explorer: Optional[SlotExplorer], screen_reader,
                 known_min_bet: Optional[Decimal] = None, demo: bool = True, gpt_client=None):
        self.memory = memory
        self.explorer = explorer
        self.screen_reader = screen_reader
        self.known_min_bet = known_min_bet
        self.demo = demo
        self.gpt_client = gpt_client
        self.status: Optional[str] = None
        self.reexplorations = 0

    # ---- API ---------------------------------------------------------------------------------------------------
    def prepare(self, device_id: str, slot_name: str) -> Result[SetupOutcome]:
        size = self._screen_size(device_id)
        if size is None:
            return Result.fail("schermo non leggibile", "SETUP_SCREENSHOT_FAILED")
        loaded = self.memory.load(slot_name, size)
        if loaded.success:
            slot_map = loaded.data
            if self.known_min_bet is not None and slot_map.min_bet != str(self.known_min_bet):
                slot_map.min_bet, slot_map.min_bet_proven = str(self.known_min_bet), True
                self.memory.save(slot_map)
            self.memory.apply_to_memory(slot_map)
            self.status = USED
            logger.info("Slot memory: mappa di '%s' trovata (esplorata il %s): uso le coordinate salvate, nessuna "
                        "chiamata GPT per orientarmi.", slot_name, slot_map.explored_at[:10] or "?")
            return Result.ok(SetupOutcome(USED, 0, slot_map))
        return self._explore(device_id, slot_name, size, base=None, only=None)

    def reexplore(self, device_id: str, slot_name: str, element: str) -> Result[bool]:
        """Un punto salvato non funziona piu': si rifa' solo quella parte ("spin" | "bet_menu"). Una volta per
        elemento e per sessione (chi chiama conta)."""
        size = self._screen_size(device_id)
        if size is None or self.explorer is None:
            return Result.fail("ri-esplorazione impossibile", "SETUP_UNAVAILABLE")
        loaded = self.memory.load(slot_name, size)
        outcome = self._explore(device_id, slot_name, size, base=loaded.data if loaded.success else None, only=element)
        self.reexplorations += 1
        return Result.ok(True) if outcome.success and outcome.data.slot_map is not None else Result.fail(
            outcome.error if not outcome.success else "ri-esplorazione senza mappa", "SETUP_REEXPLORE_FAILED")

    # ---- interni -----------------------------------------------------------------------------------------------
    def _explore(self, device_id: str, slot_name: str, size: tuple, base, only) -> Result[SetupOutcome]:
        if self.explorer is None:
            self.status = NOT_MAPPED
            return Result.ok(SetupOutcome(NOT_MAPPED))
        calls_before = self._calls()
        explored = self.explorer.explore(device_id, slot_name, demo=self.demo, known_min_bet=self.known_min_bet,
                                         base=base, only=only)
        calls = self._calls() - calls_before
        if not explored.success:
            logger.warning("Slot memory: esplorazione di '%s' non riuscita (%s): %s. Proseguo come prima.",
                           slot_name, explored.error_code, explored.error)
            self.status = NOT_MAPPED
            return Result.ok(SetupOutcome(NOT_MAPPED, calls))
        saved = self.memory.save(explored.data)
        if not saved.success:
            logger.warning("Slot memory: mappa non salvata (%s).", saved.error)
        self.memory.apply_to_memory(explored.data)
        self.status = EXPLORED
        logger.info("Slot memory: ESPLORATA '%s' con %d chiamate (spin %s, menu puntata %s, chiusura %s).",
                    slot_name, calls, "ok" if explored.data.spin else "?", explored.data.bet_menu_type,
                    explored.data.close_method)
        return Result.ok(SetupOutcome(EXPLORED, calls, explored.data))

    def _calls(self) -> int:
        usage = getattr(self.gpt_client, "_usage_by_purpose", None)
        return int(sum(usage.values())) if isinstance(usage, dict) else 0

    def _screen_size(self, device_id: str) -> Optional[tuple]:
        shot = self.screen_reader._screenshot(device_id)
        if not shot.success:
            return None
        try:
            with Image.open(shot.data) as image:
                return image.size
        except OSError:
            return None
