from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_element_memory
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)
from orchestrator.tests.validation.test_slot_autonomous import load_slot_memory

# Solo questi due nomi mappano sullo schema chiuso a 4 campi di
# test_slot_autonomous.py (mai modificato). Ogni altro elemento usa il file
# satellite slot_memory/<slot>.autopilot.json (vedi autopilot_memory.py).
_LEGACY_MEMORY_FIELDS = {
    "spin": "known_spin_coords",
    "bet_menu_toggle": "known_bet_menu_toggle",
}


class MemoryStrategy(LocatorStrategy):
    """Seconda scelta della catena: una coordinata gia' verificata in una
    sessione precedente per QUESTA slot."""
    name = "memory"

    def __init__(self, memory_dir: str = "slot_memory"):
        self.memory_dir = memory_dir

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        legacy_field = _LEGACY_MEMORY_FIELDS.get(element_name)
        if legacy_field is not None:
            memory = load_slot_memory(context.slot_name)
            coords = memory.get(legacy_field)
            if coords is None:
                return Result.fail(
                    f"No memorized coordinates for '{element_name}'", "MEMORY_ELEMENT_UNKNOWN",
                )
            x, y = coords
            return Result.ok(LocateCandidate(x=x, y=y, confidence=0.9, strategy_name=self.name))

        memory_result = load_autopilot_element_memory(context.slot_name, self.memory_dir)
        if not memory_result.success:
            return Result.fail(memory_result.error, memory_result.error_code)
        element = memory_result.data.get("elements", {}).get(element_name)
        if element is None:
            return Result.fail(
                f"No memorized coordinates for '{element_name}'", "MEMORY_ELEMENT_UNKNOWN",
            )
        return Result.ok(LocateCandidate(
            x=element["x"], y=element["y"], confidence=0.9, strategy_name=self.name,
        ))
