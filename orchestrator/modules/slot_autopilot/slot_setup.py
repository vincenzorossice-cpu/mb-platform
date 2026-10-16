"""Preparazione di una slot prima di giocare: la mappa salvata c'e' -> si usa (zero GPT); non c'e' -> la esplora Sol, la
si salva e la si usa. Il travaso nella memoria che il motore gia' consulta e' in SlotMemory.apply_to_memory."""
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_value
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
        self.button = None        # SpinButtonState (collegato dalla radice di composizione)
        self.reexplorations = 0
        self._outcomes = {}       # (dispositivo, slot) -> esito di prepare: la seconda chiamata non rifa' niente
        self._maps = {}           # slot -> ultima mappa nota (per confirm_spin senza altri screenshot)

    # ---- API ---------------------------------------------------------------------------------------------------
    def prepare(self, device_id: str, slot_name: str) -> Result[SetupOutcome]:
        """Idempotente per (dispositivo, slot): la prima chiamata carica o esplora, le altre ritornano l'esito."""
        key = (device_id, slot_name)
        if key in self._outcomes:
            return self._outcomes[key]
        outcome = self._prepare(device_id, slot_name)
        if outcome.success:
            self._outcomes[key] = outcome
        return outcome

    def verified_spin_point(self, slot_name: str) -> Optional[tuple]:
        """Punto dello spin verificato nella mappa (None se non c'e' o e' ancora in attesa di conferma)."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None or slot_map.spin is None or not slot_map.spin.verified:
            return None
        return slot_map.spin.x, slot_map.spin.y

    def spin_point(self, slot_name: str) -> Optional[tuple]:
        """Punto dello spin noto per la slot, verificato o in attesa di conferma (soldi veri)."""
        slot_map = self._maps.get(slot_name)
        return (slot_map.spin.x, slot_map.spin.y) if slot_map is not None and slot_map.spin is not None else None

    def notes(self, slot_name: str) -> list:
        slot_map = self._maps.get(slot_name)
        return list(slot_map.notes or []) if slot_map is not None else []

    def add_note(self, slot_name: str, text: str) -> Result[bool]:
        """Una regola appresa per la slot (es. come gestire un bonus): nella mappa, senza doppioni, max 10."""
        slot_map = self._maps.get(slot_name)
        text = " ".join(str(text).split())[:300]
        if slot_map is None or not text:
            return Result.ok(False)
        notes = list(slot_map.notes or [])
        if text in notes:
            return Result.ok(False)
        slot_map.notes = (notes + [text])[-10:]
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def learn_bet_menu(self, device_id: str, slot_name: str, final_bet=None) -> Result[bool]:
        """Dopo il primo minimo impostato dal motore della puntata: nella mappa entrano il verso di scorrimento del
        carosello e il MINIMO (provato se il motore l'ha dimostrato: `min_bet_trusted`; altrimenti solo la puntata
        raggiunta, come suggerimento). Le run successive lo caricano senza cercarlo."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return Result.ok(False)
        changed = False
        direction = load_autopilot_value(slot_name, "bet_row_direction", self.memory.memory_dir)
        if direction in ("left", "right") and slot_map.bet_scroll_direction != direction:
            slot_map.bet_scroll_direction, changed = direction, True
        trusted = load_autopilot_value(slot_name, "min_bet_trusted", self.memory.memory_dir)
        if trusted:
            if slot_map.min_bet != str(trusted) or not slot_map.min_bet_proven:
                slot_map.min_bet, slot_map.min_bet_proven, changed = str(trusted), True, True
        elif final_bet is not None and slot_map.min_bet is None:
            slot_map.min_bet, slot_map.min_bet_proven, changed = str(final_bet), False, True
        if not changed:
            return Result.ok(False)
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    FREE_SPINS_NOTE = ("This game has free spins rounds: during them the balance does not drop when a spin starts and the "
                       "spin button may need to be pressed for every free spin.")

    def quick_stop(self, slot_name: str) -> Optional[bool]:
        """True/False se il Quick Stop e' gia' stato provato su questa slot, None se non testato."""
        slot_map = self._maps.get(slot_name)
        return slot_map.quick_stop if slot_map is not None and slot_map.quick_stop_tested else None

    def set_quick_stop(self, slot_name: str, enabled: bool, reason: str) -> Result[bool]:
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return Result.ok(False)
        slot_map.quick_stop, slot_map.quick_stop_tested, slot_map.quick_stop_reason = bool(enabled), True, reason[:120]
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def mark_free_spins(self, slot_name: str) -> Result[bool]:
        """La slot ha giri gratuiti: si ricorda nella mappa (e come nota per il vigile)."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None or slot_map.has_free_spins:
            return Result.ok(False)
        slot_map.has_free_spins = True
        slot_map.notes = (list(slot_map.notes or []) + [self.FREE_SPINS_NOTE])[-10:] \
            if self.FREE_SPINS_NOTE not in (slot_map.notes or []) else slot_map.notes
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def learn_spin_method(self, slot_name: str, method: Optional[str]) -> Result[bool]:
        """Quale segnale ha funzionato per capire che lo spin e' finito (freccia, colore, luminosita', disegno, o il
        confronto della zona dei rulli): nella mappa, cosi' le run successive non rifanno i tentativi."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None or method not in ("arrow", "color", "luminance", "patch", "frame_zone") \
                or slot_map.spin_ready_method == method:
            return Result.ok(False)
        slot_map.spin_ready_method = method
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    # ---- autospin e turbo ---------------------------------------------------------------------------------------
    def autospin_info(self, slot_name: str):
        slot_map = self._maps.get(slot_name)
        return slot_map.autospin if slot_map is not None else None

    def turbo_info(self, slot_name: str):
        slot_map = self._maps.get(slot_name)
        return slot_map.turbo if slot_map is not None else None

    def save_autospin(self, slot_name: str, info) -> Result[bool]:
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return Result.ok(False)
        slot_map.autospin = info
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def save_turbo(self, slot_name: str, info) -> Result[bool]:
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return Result.ok(False)
        slot_map.turbo = info
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def gamble_info(self, slot_name: str):
        """((x, y), firma) del pulsante Raccogli del popup di raddoppio gia' imparato, o None."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None or not slot_map.gamble_popup or not slot_map.collect_coords:
            return None
        return tuple(slot_map.collect_coords), slot_map.gamble_signature

    def save_gamble(self, slot_name: str, coords, signature) -> Result[bool]:
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return Result.ok(False)
        slot_map.gamble_popup, slot_map.collect_coords = True, [int(coords[0]), int(coords[1])]
        slot_map.gamble_signature = list(signature) if signature else slot_map.gamble_signature
        saved = self.memory.save(slot_map)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def explore_autospin(self, device_id: str, slot_name: str) -> Result:
        """Esplora l'autospin una volta per slot (e per schermo), SOLO in demo e dopo la puntata minima. Una mappa gia'
        con esito (present/absent) o con troppi tentativi a vuoto non si riesplora. Salva l'esito nella mappa."""
        from orchestrator.modules.slot_autopilot.autospin_manager import MAX_UNKNOWN_ATTEMPTS
        current = self.autospin_info(slot_name)
        if current is not None and (current.status != "unknown" or current.attempts >= MAX_UNKNOWN_ATTEMPTS):
            return Result.ok(current)
        if not self.demo:
            return Result.fail("l'autospin si esplora solo in demo", "AUTOSPIN_NOT_DEMO")
        if self.explorer is None or self._maps.get(slot_name) is None:
            return Result.fail("nessuna mappa o esploratore", "AUTOSPIN_NO_MAP")
        slot_map = self._maps[slot_name]
        panel = slot_map.bet_menu_open
        found = self.explorer.explore_autospin(
            device_id, slot_name, self.spin_point(slot_name), (panel.x, panel.y) if panel is not None else None, current)
        if not found.success:
            return found
        saved = self.save_autospin(slot_name, found.data)
        if not saved.success:
            logger.warning("Autospin: esito non salvato: %s", saved.error)
        return found

    def explore_turbo(self, device_id: str, slot_name: str) -> Result:
        """Cerca e ATTIVA il turbo (solo demo). La conferma viene da `conclude_turbo` con i tempi misurati."""
        from orchestrator.modules.slot_autopilot.turbo_manager import MAX_ATTEMPTS
        current = self.turbo_info(slot_name)
        if current is not None and (current.status != "unknown" or current.attempts >= MAX_ATTEMPTS):
            return Result.ok(current)
        if not self.demo:
            return Result.fail("il turbo si esplora solo in demo", "TURBO_NOT_DEMO")
        if self.explorer is None or self._maps.get(slot_name) is None:
            return Result.fail("nessuna mappa o esploratore", "TURBO_NO_MAP")
        return self.explorer.explore_turbo(device_id, self.spin_point(slot_name), current)

    def conclude_turbo(self, device_id: str, slot_name: str, info, without, with_turbo):
        """Conferma o scarta il turbo dai tempi misurati e salva l'esito."""
        manager = getattr(self.explorer, "turbo_manager", None)
        if manager is None:
            return info
        final = manager.conclude(device_id, info, without, with_turbo)
        self.save_turbo(slot_name, final)
        return final

    def reseed(self, slot_name: str) -> bool:
        """Dopo un rientro: rimette in memoria le coordinate della mappa (alcune possono essere state dimenticate o
        sovrascritte durante la sessione)."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None:
            return False
        return self.memory.apply_to_memory(slot_map).success

    def confirm_spin(self, device_id: str, slot_name: str) -> Result[bool]:
        """Soldi veri: il primo spin vero ha addebitato, quindi il punto di Sol e' verificato. Salva la mappa."""
        slot_map = self._maps.get(slot_name)
        if slot_map is None or not slot_map.spin_pending or slot_map.spin is None:
            return Result.ok(False)
        slot_map.spin.verified, slot_map.spin_pending = True, False
        saved = self.memory.save(slot_map)
        if saved.success:
            logger.info("Slot memory: lo spin di '%s' e' confermato dal primo spin vero: mappa verificata.", slot_name)
        return Result.ok(True) if saved.success else Result.fail(saved.error, saved.error_code)

    def _prepare(self, device_id: str, slot_name: str) -> Result[SetupOutcome]:
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
            self._maps[slot_name] = slot_map
            if slot_map.spin_ready_method == "frame_zone" and self.button is not None:
                self.button.mark_unusable(slot_name, size)   # niente piu' i 3 tentativi falliti di apprendimento
            self.status = USED
            logger.info("Slot memory: mappa di '%s' trovata (esplorata il %s): uso le coordinate salvate, nessuna "
                        "chiamata GPT per orientarmi.", slot_name, slot_map.explored_at[:10] or "?")
            logger.info("Slot memory: %s.", slot_map.summary())
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
        self._outcomes = {k: v for k, v in self._outcomes.items() if k[1] != slot_name}
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
        self._maps[slot_name] = explored.data
        self.status = EXPLORED
        logger.info("Slot memory: ESPLORATA '%s' con %d chiamate.", slot_name, calls)
        logger.info("Slot memory: %s.", explored.data.summary())
        for note in getattr(self.explorer, "last_notes", []) or []:
            logger.warning("Slot memory: %s", note)
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
