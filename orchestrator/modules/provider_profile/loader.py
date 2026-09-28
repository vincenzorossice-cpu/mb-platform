import json
import logging
import os
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.profile import (
    ElementSpec,
    MacroSpec,
    ProviderProfile,
    StateSpec,
)

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())
    return safe or "unknown"


class ProviderProfileLoader:
    """Carica, valida e converte le coordinate normalizzate di un ProviderProfile
    (JSON curato a mano in provider_profiles/<slug>.json)."""

    def __init__(self, profiles_dir: str = "provider_profiles"):
        self.profiles_dir = profiles_dir

    def _path(self, slot_name: str) -> str:
        return os.path.join(self.profiles_dir, f"{_slugify(slot_name)}.json")

    def load(self, slot_name: str) -> Result[ProviderProfile]:
        path = self._path(slot_name)
        if not os.path.exists(path):
            return Result.fail(f"Profile not found: {path}", "PROFILE_NOT_FOUND")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return Result.fail(f"Invalid profile JSON: {exc}", "PROFILE_INVALID_JSON")

        try:
            elements = {
                name: ElementSpec(name=name, **spec)
                for name, spec in data.get("elements", {}).items()
            }
            states = {
                name: StateSpec(name=name, **spec)
                for name, spec in data.get("states", {}).items()
            }
            macros = {
                name: MacroSpec(name=name, **spec)
                for name, spec in data.get("macros", {}).items()
            }
            profile = ProviderProfile(
                slot_name=data.get("slot_name", slot_name),
                provider=data.get("provider", ""),
                screen_width=data.get("screen_width", 0),
                screen_height=data.get("screen_height", 0),
                elements=elements,
                states=states,
                macros=macros,
                known_min_bet=data.get("known_min_bet"),
                verified=data.get("verified", False),
                canvas_bbox=tuple(data["canvas_bbox"]) if data.get("canvas_bbox") else None,
            )
        except TypeError as exc:
            return Result.fail(f"Invalid profile JSON: {exc}", "PROFILE_INVALID_JSON")

        validation = self.validate(profile)
        if not validation.success:
            return Result.fail(validation.error, validation.error_code)
        return Result.ok(profile)

    def validate(self, profile: ProviderProfile) -> Result[bool]:
        if not profile.slot_name:
            return Result.fail("slot_name is empty", "PROFILE_INVALID_ELEMENT")
        if profile.screen_width <= 0 or profile.screen_height <= 0:
            return Result.fail(
                f"Invalid screen dimensions: {profile.screen_width}x{profile.screen_height}",
                "PROFILE_INVALID_ELEMENT",
            )
        for element in profile.elements.values():
            has_norm = element.norm_x is not None and element.norm_y is not None
            has_any_locator = (
                has_norm or element.ocr_labels or element.template_path or element.region_hint
            )
            if not has_any_locator:
                return Result.fail(
                    f"Element '{element.name}' has no way to be located",
                    "PROFILE_INVALID_ELEMENT",
                )
            if element.norm_x is not None and not (0.0 <= element.norm_x <= 1.0):
                return Result.fail(
                    f"Element '{element.name}' norm_x out of range: {element.norm_x}",
                    "PROFILE_INVALID_ELEMENT",
                )
            if element.norm_y is not None and not (0.0 <= element.norm_y <= 1.0):
                return Result.fail(
                    f"Element '{element.name}' norm_y out of range: {element.norm_y}",
                    "PROFILE_INVALID_ELEMENT",
                )
        for macro in profile.macros.values():
            for step in macro.steps:
                if step not in profile.elements:
                    return Result.fail(
                        f"Macro '{macro.name}' references unknown element '{step}'",
                        "PROFILE_INVALID_MACRO",
                    )
        if profile.canvas_bbox is not None:
            if len(profile.canvas_bbox) != 4:
                return Result.fail(
                    f"canvas_bbox must have 4 values, got {profile.canvas_bbox}",
                    "PROFILE_INVALID_CANVAS_BBOX",
                )
            x1, y1, x2, y2 = profile.canvas_bbox
            if x1 >= x2 or y1 >= y2:
                return Result.fail(
                    f"canvas_bbox has non-positive width/height: {profile.canvas_bbox}",
                    "PROFILE_INVALID_CANVAS_BBOX",
                )
            if x1 < 0 or y1 < 0 or x2 > profile.screen_width or y2 > profile.screen_height:
                return Result.fail(
                    f"canvas_bbox {profile.canvas_bbox} out of screen bounds "
                    f"{profile.screen_width}x{profile.screen_height}",
                    "PROFILE_INVALID_CANVAS_BBOX",
                )
        return Result.ok(True)

    def to_abs(
        self, element: ElementSpec, screen_width: int, screen_height: int,
        canvas_bbox: Optional[tuple] = None,
    ) -> Result[tuple]:
        if element.norm_x is None or element.norm_y is None:
            return Result.fail(
                f"Element '{element.name}' has no normalized coordinates",
                "ELEMENT_HAS_NO_NORM_COORDS",
            )
        if canvas_bbox is not None:
            x1, y1, x2, y2 = canvas_bbox
            x = x1 + round(element.norm_x * (x2 - x1))
            y = y1 + round(element.norm_y * (y2 - y1))
            return Result.ok((x, y))
        x = round(element.norm_x * screen_width)
        y = round(element.norm_y * screen_height)
        return Result.ok((x, y))

    def to_norm(
        self, x: int, y: int, screen_width: int, screen_height: int,
        canvas_bbox: Optional[tuple] = None,
    ) -> Result[tuple]:
        if canvas_bbox is not None:
            x1, y1, x2, y2 = canvas_bbox
            width, height = x2 - x1, y2 - y1
            if width <= 0 or height <= 0:
                return Result.fail(
                    f"Invalid canvas_bbox dimensions: {canvas_bbox}", "INVALID_SCREEN_DIMENSIONS",
                )
            return Result.ok(((x - x1) / width, (y - y1) / height))
        if screen_width <= 0 or screen_height <= 0:
            return Result.fail(
                f"Invalid screen dimensions: {screen_width}x{screen_height}",
                "INVALID_SCREEN_DIMENSIONS",
            )
        return Result.ok((x / screen_width, y / screen_height))
