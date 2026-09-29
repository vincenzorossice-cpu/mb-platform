import logging
from dataclasses import dataclass
from typing import Callable, Optional

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.frame_diff import diff_score
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, Expectation

logger = logging.getLogger(__name__)

_CLOSE_ELEMENT = "close_button"
# Chiusura vera e propria, in ordine di preferenza. Le voci di "uscita" sono
# l'ultima risorsa e si cercano SOLO quando lo schermo e' gia' diverso
# dall'originale (sulla schermata di gioco "Esci" lascerebbe la slot).
_CLOSE_WORDS = ("X", "×", "✕", "✖", "CHIUDI", "CLOSE")
_EXIT_WORDS = ("ESCI", "EXIT", "INDIETRO", "BACK")


@dataclass
class RecoveryOutcome:
    method: str  # "already_original" | "ocr_close" | "gpt_close" | "retap"
    attempts: int


class ScreenRecovery:
    """Riporta lo schermo a com'era prima di un tap sbagliato che ha aperto
    un'altra schermata (info, regole, impostazioni...). "Tornati" = lo schermo
    e' di nuovo uguale all'originale: non serve sapere COSA e' la schermata
    aperta. Vie, in ordine: un testo/icona di chiusura letto con l'OCR, una X
    trovata da GPT, il ritocco della stessa icona che l'aveva aperta. Niente
    tasto Android BACK: in un gioco dentro Chrome potrebbe uscire dalla pagina."""

    def __init__(self, locator: ElementLocator, verifier: ActionVerifier, config: AutoPilotConfig):
        self.locator = locator
        self.verifier = verifier
        self.config = config

    def recover(
        self, device_id: str, original_screenshot: str, tapped_xy: tuple,
        context_provider: Callable[[], Result],
    ) -> Result[RecoveryOutcome]:
        try:
            return self._recover(device_id, original_screenshot, tapped_xy, context_provider)
        except Exception as exc:
            return Result.fail(f"Unexpected error during recovery: {exc}", "RECOVERY_ERROR")

    def _recover(self, device_id, original_screenshot, tapped_xy, context_provider) -> Result[RecoveryOutcome]:
        shot = self.verifier.screen_reader._screenshot(device_id)
        if not shot.success:
            return Result.fail(shot.error, shot.error_code)
        if self._is_original(original_screenshot, shot.data):
            logger.info("recovery: lo schermo e' gia' quello originale, nulla da fare.")
            return Result.ok(RecoveryOutcome(method="already_original", attempts=0))

        context_result = context_provider()
        if not context_result.success:
            return Result.fail(context_result.error, context_result.error_code)
        context = context_result.data

        attempts = 0
        tried = []  # punti gia' tappati in questa recovery
        for method, point in self._candidates(context, tapped_xy, context_provider):
            if attempts >= self.config.max_recovery_attempts:
                break
            if any(abs(point[0] - tx) < 30 and abs(point[1] - ty) < 30 for tx, ty in tried):
                continue
            attempts += 1
            tried.append(point)
            logger.info("recovery: tentativo %d/%d via %s a (%d,%d)", attempts,
                        self.config.max_recovery_attempts, method, point[0], point[1])
            tap = self.verifier.verified_tap(device_id, point[0], point[1], Expectation(kind="screen_changed"))
            if tap.success and tap.data.screenshot_after and self._is_original(
                original_screenshot, tap.data.screenshot_after,
            ):
                logger.info("recovery: schermo originale ripristinato via %s", method)
                return Result.ok(RecoveryOutcome(method=method, attempts=attempts))

        return Result.fail(
            f"Could not return to the original screen after {attempts} attempts", "RECOVERY_FAILED",
        )

    def _candidates(self, context, tapped_xy, context_provider):
        """Generatore pigro: la ricerca GPT (costosa) parte solo se i tentativi
        precedenti non hanno funzionato."""
        ocr_points = self._ocr_close_points(context)
        for point in ocr_points:
            yield "ocr_close", point
        if ocr_points:
            # Dopo i tap di prova lo schermo puo' essere cambiato: contesto fresco.
            refreshed = context_provider()
            if refreshed.success:
                context = refreshed.data
        gpt_point = self._gpt_close_point(context)
        if gpt_point is not None:
            yield "gpt_close", gpt_point
        yield "retap", tuple(tapped_xy)

    def _ocr_close_points(self, context) -> list:
        def center(box):
            return (round((box.bbox[0] + box.bbox[2]) / 2), round((box.bbox[1] + box.bbox[3]) / 2))

        def matching(words):
            return [center(box) for box in context.text_boxes if box.text.strip().upper() in words]

        return matching(_CLOSE_WORDS) + matching(_EXIT_WORDS)

    def _gpt_close_point(self, context) -> Optional[tuple]:
        result = self.locator.locate(_CLOSE_ELEMENT, context, exclude_strategies=("hitl",))
        if not result.success:
            logger.info("recovery: nessuna X trovata da GPT (%s)", result.error_code)
            return None
        return (result.data.x, result.data.y)

    def _is_original(self, original: str, current: str) -> bool:
        diff = diff_score(original, current)
        return diff.success and diff.data < self.config.recovery_return_threshold
