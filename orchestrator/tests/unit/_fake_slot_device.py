"""Unico test-double stateful della suite slot_autopilot (vedi risoluzione #2
del piano): una slot machine finta con saldo/puntata/vincita e un set di
elementi nominati che reagiscono ai tap. Non e' raccolto da pytest (il nome
del file non inizia per 'test_').
"""
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable
from unittest.mock import MagicMock

from PIL import Image

from orchestrator.core.result import Result


@dataclass
class FakeElement:
    name: str
    x: int
    y: int
    on_tap: Callable


class FakeSlotDevice:
    """Stand-in in memoria per uno schermo slot reale: saldo, puntata, vincita,
    un dict di elementi nominati con (x,y) e una callback on_tap che muta lo
    stato. Tappare vicino alle coordinate di un elemento registrato invoca la
    sua on_tap; tappare una zona morta non cambia nulla. Produce screenshot
    PNG reali via PIL (piccole immagini a tinta unita con un pixel di rumore
    per chiamata, cosi' frame_diff/OCR possono essere esercitati senza ADB/vision,
    ma due screenshot consecutivi non sono mai byte-identici, come nella realta')."""

    def __init__(self, screenshot_dir, width=1080, height=2400,
                 balance=Decimal("10.00"), bet=Decimal("1.00")):
        self.screenshot_dir = str(screenshot_dir)
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self.width = width
        self.height = height
        self.balance = balance
        self.bet = bet
        self.win = Decimal("0.00")
        self.spin_button_visible = True
        self.elements = {}
        self._frame_count = 0

    def add_element(self, name: str, x: int, y: int, on_tap: Callable) -> None:
        self.elements[name] = FakeElement(name=name, x=x, y=y, on_tap=on_tap)

    def tap(self, x: int, y: int, tolerance: int = 15) -> bool:
        for element in self.elements.values():
            if abs(element.x - x) <= tolerance and abs(element.y - y) <= tolerance:
                element.on_tap(self)
                return True
        return False

    def screenshot(self) -> str:
        self._frame_count += 1
        path = os.path.join(self.screenshot_dir, f"frame_{self._frame_count}.png")
        img = Image.new("RGB", (self.width, self.height), color=(10, 10, 10))
        img.putpixel((0, 0), (self._frame_count % 256, 0, 0))
        img.save(path)
        return path

    def ocr_state(self) -> dict:
        return {
            "saldo": float(self.balance),
            "posta": float(self.bet),
            "vincita": float(self.win),
            "spin_button_coords": (540, 2000) if self.spin_button_visible else None,
            "is_active": True,
            "raw_texts": [],
        }

    def text_boxes(self) -> list:
        return []


def fake_screen_reader(device: FakeSlotDevice) -> MagicMock:
    reader = MagicMock()
    reader._screenshot.side_effect = lambda device_id: Result.ok(device.screenshot())
    return reader


def fake_touch_injector(device: FakeSlotDevice) -> MagicMock:
    injector = MagicMock()

    def _tap(device_id, x, y):
        device.tap(x, y)
        return Result.ok(True)

    injector.tap_coordinates.side_effect = _tap
    return injector


def fake_ocr_reader(device: FakeSlotDevice) -> MagicMock:
    ocr = MagicMock()
    ocr.read_slot_state.side_effect = lambda screenshot_path: device.ocr_state()
    ocr.read_text_boxes.side_effect = lambda screenshot_path: Result.ok(device.text_boxes())
    return ocr
