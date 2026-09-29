import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_element_memory
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader, _slugify
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.tests.validation.test_slot_autonomous import load_slot_memory

logger = logging.getLogger(__name__)

# Stessa mappatura di MemoryStrategy: solo questi due elementi vivono nello
# schema fisso di slot_memory/<slot>.json.
_LEGACY_ELEMENTS = {"spin": "known_spin_coords", "bet_menu_toggle": "known_bet_menu_toggle"}
_MENU_TOGGLE = "bet_menu_toggle"
_MENU_CELL = "bet_minus"
_COORD_TOLERANCE = 0.005  # ~5px su 1080: stessa coordinata a meno del rumore


@dataclass
class SessionEvidence:
    """Fatti osservati in una sessione, sufficienti a decidere se salvare o
    promuovere un profilo. Nessun dato specifico della slot."""
    slot_name: str
    screen_width: int
    screen_height: int
    min_bet_verified: bool
    final_bet: Optional[Decimal]
    spins_ok: int
    assisted: bool       # almeno un elemento trovato da GPT/HITL (non da profile/memory)
    no_effect_spins: int


class ProfileBuilder:
    """Trasforma cio' che l'autopilot ha imparato (slot_memory) in un
    provider_profiles/<slot>.json auto-generato. Prudente: la bozza nasce
    verified=False e diventa fidata solo dopo `promotion_threshold` sessioni
    consecutive pulite con le STESSE coordinate. Non tocca mai un profilo
    curato a mano."""

    def __init__(self, profiles_dir: str = "provider_profiles", memory_dir: str = "slot_memory",
                 promotion_threshold: int = 2):
        self.profiles_dir = profiles_dir
        self.memory_dir = memory_dir
        self.promotion_threshold = promotion_threshold
        self._loader = ProviderProfileLoader(profiles_dir=profiles_dir)

    def record_session(self, evidence: SessionEvidence) -> Result[str]:
        """Ritorna l'esito: skipped_curated | already_verified |
        insufficient_evidence | draft_written | counter_reset | promoted."""
        existing_result = self._loader.load(evidence.slot_name)
        existing: Optional[ProviderProfile] = None
        if existing_result.success:
            existing = existing_result.data
            if not existing.auto_generated:
                return Result.ok("skipped_curated")
            if existing.verified:
                return Result.ok("already_verified")
        elif existing_result.error_code != "PROFILE_NOT_FOUND":
            # File presente ma illeggibile: meglio non sovrascriverlo alla cieca.
            return Result.fail(existing_result.error, existing_result.error_code)

        elements = self._elements_from_memory(evidence)
        solid = evidence.min_bet_verified and evidence.spins_ok > 0 and bool(elements)
        if not solid:
            if existing is None:
                return Result.ok("insufficient_evidence")
            return self._write(evidence, existing.elements, existing.known_min_bet, 0, False, existing, "counter_reset")

        known_min_bet = float(evidence.final_bet) if evidence.final_bet is not None else None
        clean = (
            existing is not None
            and not evidence.assisted
            and evidence.no_effect_spins == 0
            and known_min_bet == existing.known_min_bet
            and self._same_coordinates(existing.elements, elements)
        )
        sessions_ok = existing.auto_sessions_ok + 1 if clean else 0
        verified = sessions_ok >= self.promotion_threshold
        outcome = "promoted" if verified else "draft_written"
        return self._write(evidence, elements, known_min_bet, sessions_ok, verified, existing, outcome)

    def _elements_from_memory(self, evidence: SessionEvidence) -> dict:
        width, height = evidence.screen_width, evidence.screen_height
        if width <= 0 or height <= 0:
            return {}
        coords = {}
        memory_result = load_autopilot_element_memory(evidence.slot_name, self.memory_dir)
        if memory_result.success:
            for name, item in memory_result.data.get("elements", {}).items():
                coords[name] = (item["x"], item["y"])
        legacy = load_slot_memory(evidence.slot_name)
        for name, field_name in _LEGACY_ELEMENTS.items():
            if legacy.get(field_name):
                coords[name] = tuple(legacy[field_name])

        if _MENU_TOGGLE in coords:
            # Con un menu puntata, la cella "bet_minus" appartiene al menu
            # APERTO: come elemento di profilo verrebbe tappata a menu chiuso
            # (no_effect). Meglio ometterla: BetController apre il menu da solo
            # quando il profilo non ha bet_minus.
            coords.pop(_MENU_CELL, None)

        elements = {}
        for name, (x, y) in coords.items():
            norm = self._loader.to_norm(x, y, width, height)
            if not norm.success:
                continue
            norm_x, norm_y = norm.data
            if not (0.0 <= norm_x <= 1.0 and 0.0 <= norm_y <= 1.0):
                continue
            elements[name] = ElementSpec(
                name=name, norm_x=round(norm_x, 6), norm_y=round(norm_y, 6),
                notes="Auto-generato da coordinate verificate dal vivo in sessioni reali.",
            )
        return elements

    @staticmethod
    def _same_coordinates(previous: dict, current: dict) -> bool:
        if set(previous) != set(current):
            return False
        return all(
            abs(previous[name].norm_x - current[name].norm_x) <= _COORD_TOLERANCE
            and abs(previous[name].norm_y - current[name].norm_y) <= _COORD_TOLERANCE
            for name in previous
        )

    def _write(self, evidence, elements, known_min_bet, sessions_ok, verified,
               existing: Optional[ProviderProfile], outcome: str) -> Result[str]:
        data = {
            "slot_name": evidence.slot_name,
            "provider": existing.provider if existing else "",
            "screen_width": evidence.screen_width,
            "screen_height": evidence.screen_height,
            "verified": verified,
            "auto_generated": True,
            "auto_sessions_ok": sessions_ok,
            "canvas_bbox": list(existing.canvas_bbox) if existing and existing.canvas_bbox else None,
            "known_min_bet": known_min_bet,
            "elements": {
                name: {"norm_x": spec.norm_x, "norm_y": spec.norm_y, "notes": spec.notes}
                for name, spec in elements.items()
            },
            "states": {},
            "macros": {},
            "_notes": (
                "Profilo AUTO-GENERATO. Diventa verified dopo "
                f"{self.promotion_threshold} sessioni consecutive pulite con le stesse coordinate; "
                "un aiuto GPT/HITL, un tap a vuoto o una coordinata diversa azzerano il conteggio."
            ),
        }
        path = os.path.join(self.profiles_dir, f"{_slugify(evidence.slot_name)}.json")
        try:
            os.makedirs(self.profiles_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            return Result.fail(f"Cannot write profile: {exc}", "PROFILE_WRITE_ERROR")
        logger.info("Profilo auto-generato '%s': %s (sessioni pulite %d, verified=%s)",
                    evidence.slot_name, outcome, sessions_ok, verified)
        return Result.ok(outcome)
