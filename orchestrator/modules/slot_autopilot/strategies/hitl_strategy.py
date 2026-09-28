from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)
from orchestrator.tests.validation.test_slot_autonomous import handle_hitl_escalation


class HITLStrategy(LocatorStrategy):
    """Ultima istanza della catena. handle_hitl_escalation tappa e verifica
    GIA' da sola (l'operatore vede l'effetto del proprio comando prima che la
    funzione ritorni), quindi il candidato arriva con verified=True: chi
    chiama (ElementLocator/BetController/SpinController) non deve richiamare
    ActionVerifier su un candidato HITL."""
    name = "hitl"

    def __init__(self, screen_reader, ocr_reader, touch_injector):
        self.screen_reader = screen_reader
        self.ocr_reader = ocr_reader
        self.touch_injector = touch_injector

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        current_bet = context.slot_ocr_state.get("posta")
        known_min_bet = context.profile.known_min_bet if context.profile else None

        action, _data, _confirmed_min_bet, tapped_coords = handle_hitl_escalation(
            self.screen_reader, self.ocr_reader, self.touch_injector,
            context.device_id, current_bet, known_min_bet,
        )

        if action == "stop":
            return Result.fail("Operator requested stop", "HITL_STOP_REQUESTED")
        if action == "skip":
            return Result.fail("Operator requested skip", "HITL_SKIPPED")
        if action == "hint":
            return Result.fail(f"Operator hint only, no tap: {_data}", "HITL_HINT_ONLY")
        if action in ("resolved", "spin_confirmed"):
            if tapped_coords is None:
                return Result.fail("HITL resolved without coordinates", "HITL_NO_COORDS")
            x, y = tapped_coords
            return Result.ok(LocateCandidate(
                x=x, y=y, confidence=1.0, strategy_name=self.name, verified=True,
            ))
        return Result.fail(f"Unexpected HITL outcome: {action}", "HITL_UNEXPECTED_OUTCOME")
