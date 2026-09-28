from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import (
    LocateCandidate,
    LocateContext,
    LocatorStrategy,
)


class TemplateStrategy(LocatorStrategy):
    """Quinta scelta: template matching via OpenCV. Import lazy (come
    SlotOCRReader fa con easyocr): nessun costo per chi non usa mai questa
    strategia, e opencv-python-headless e' l'unico consumatore che la
    giustifica come dipendenza del progetto."""
    name = "template"

    def __init__(self, min_match_score: float = 0.75):
        self.min_match_score = min_match_score

    def locate(self, element_name: str, context: LocateContext) -> Result[LocateCandidate]:
        element = context.profile.elements.get(element_name) if context.profile else None
        template_path = element.template_path if element else None
        if not template_path:
            return Result.fail(f"No template_path configured for '{element_name}'", "TEMPLATE_NOT_CONFIGURED")

        try:
            import cv2
        except ImportError as exc:
            return Result.fail(f"opencv not available: {exc}", "TEMPLATE_ERROR")

        try:
            screen_img = cv2.imread(context.screenshot_path, cv2.IMREAD_GRAYSCALE)
            template_img = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
            if screen_img is None or template_img is None:
                return Result.fail(f"Cannot read image for template match: {template_path}", "TEMPLATE_IMAGE_READ_ERROR")

            result = cv2.matchTemplate(screen_img, template_img, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
            if max_val < self.min_match_score:
                return Result.fail(
                    f"Template match score {max_val:.2f} below threshold {self.min_match_score}",
                    "TEMPLATE_MATCH_BELOW_THRESHOLD",
                )
            template_h, template_w = template_img.shape[:2]
            center_x = max_loc[0] + template_w // 2
            center_y = max_loc[1] + template_h // 2
            return Result.ok(LocateCandidate(
                x=center_x, y=center_y, confidence=float(max_val), strategy_name=self.name,
            ))
        except Exception as exc:
            return Result.fail(f"Template matching error: {exc}", "TEMPLATE_ERROR")
