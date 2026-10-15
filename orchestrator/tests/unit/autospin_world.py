"""Telefono finto per autospin/turbo: un piccolo stato (menu, pannello, autospin attivo, popup) che si legge come farebbe
il bot: screenshot (un colore per stato), box OCR, saldo e freccia dello spin. Nessuna rete, nessuno sleep vero."""
from decimal import Decimal
from types import SimpleNamespace

from PIL import Image

from orchestrator.core.result import Result

W, H = 400, 800
SPIN = (200, 700)
AUTO = (340, 120)
MENU = (380, 40)
AUTO_IN_MENU = (200, 300)
OPTION_INF = (200, 450)
CONFIRM = (200, 560)
STOP_BUTTON = (340, 700)
CENTER = (W // 2, H // 2)


def _box(text, xy, w=60, h=30):
    return SimpleNamespace(text=text, bbox=(xy[0] - w // 2, xy[1] - h // 2, xy[0] + w // 2, xy[1] + h // 2),
                           confidence=0.9)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class World:
    """kind: "main" (AUTO sulla schermata principale, apre un pannello), "direct" (AUTO parte da solo),
    "menu" (AUTO dentro il menu), "none" (nessun autospin). stop: "tap_spin" | "tap_stop" | "never"."""

    def __init__(self, tmp_path, kind="main", stop="tap_spin", balance="100.00", bet="0.10", fixed=False):
        self.tmp, self.kind, self.stop, self.fixed = tmp_path, kind, stop, fixed
        self.balance, self.bet = Decimal(balance), Decimal(bet)
        self.menu_open = self.panel_open = self.running = self.popup = self.turbo_on = False
        self.taps, self.shots, self.counter = [], 0, None
        self.states = {}
        self.stall_after_shots = None            # dopo N screenshot con l'autospin attivo si ferma da solo
        self.running_shots = 0
        self.screen_reader = SimpleNamespace(_screenshot=self._screenshot)
        self.touch_injector = SimpleNamespace(tap_coordinates=self._tap)
        self.ocr = SimpleNamespace(read_text_boxes=self._boxes, read_slot_state=self._state)
        self.button = SimpleNamespace(classify=self._classify)

    # ---- il mondo -----------------------------------------------------------------------------------------------------
    def _screenshot(self, device):
        self.shots += 1
        if self.running:
            self.running_shots += 1
            self.balance -= Decimal("0.01")
            if self.stall_after_shots and self.running_shots >= self.stall_after_shots:
                self.running, self.running_shots = False, 0
        color = (30 + 40 * self.menu_open + 50 * self.turbo_on, 30 + 60 * self.panel_open, 30 + 90 * self.running, 255)
        color = (color[0], color[1], color[2] + (60 if self.popup else 0))
        image = Image.new("RGB", (W, H), color[:3])
        path = str(self.tmp / f"w{self.shots}.png")
        image.save(path)
        self.states[path] = dict(menu=self.menu_open, panel=self.panel_open, running=self.running, popup=self.popup,
                                 balance=self.balance, tick=self.shots, counter=self.counter)
        return Result.ok(path)

    def _near(self, xy, target, r=45):
        return abs(xy[0] - target[0]) <= r and abs(xy[1] - target[1]) <= r

    def _tap(self, device, x, y):
        xy = (x, y)
        self.taps.append(xy)
        if self.popup and self._near(xy, CENTER, 150):
            self.popup, self.running = False, False               # il popup si chiude e l'autospin era fermo
            return Result.ok(True)
        if self.running and self._near(xy, SPIN) and self.stop == "tap_spin":
            self.running = False
        elif self.running and self._near(xy, STOP_BUTTON) and self.stop == "tap_stop":
            self.running = False
        elif self.panel_open and self._near(xy, CONFIRM):
            self.panel_open, self.running, self.running_shots = False, True, 0
            self.counter = 100 if self.fixed else None
        elif self.panel_open and self._near(xy, OPTION_INF):
            pass
        elif self.kind == "main" and self._near(xy, AUTO) and not self.running:
            self.panel_open = not self.panel_open
        elif self.kind == "direct" and self._near(xy, AUTO):
            self.running = not self.running
        elif self.kind in ("turbo", "turbo_menu") and self._near(xy, AUTO if self.kind == "turbo" else AUTO_IN_MENU) \
                and (self.kind == "turbo" or self.menu_open):
            self.turbo_on = not self.turbo_on
        elif self.kind in ("menu", "turbo_menu") and self._near(xy, MENU):
            self.menu_open = not self.menu_open
        elif self.kind == "menu" and self.menu_open and self._near(xy, AUTO_IN_MENU):
            self.menu_open, self.panel_open = False, True
        return Result.ok(True)

    def _boxes(self, path):
        s = self.states[path]
        boxes = []
        if s["panel"]:
            boxes += [_box("10", (100, 450)), _box("25", (150, 450)), _box("100", (300, 450)),
                      _box("ILLIMITATO", OPTION_INF, 100), _box("AVVIA", CONFIRM)]
        elif s["menu"]:
            boxes += [_box("TURBO" if self.kind == "turbo_menu" else "AUTOSPIN", AUTO_IN_MENU, 100)]
        else:
            if self.kind in ("main", "direct"):
                boxes.append(_box("AUTO", AUTO))
            if self.kind == "turbo":
                boxes.append(_box("TURBO", AUTO))
            if self.kind in ("menu", "turbo_menu"):
                boxes.append(_box("MENU", MENU))
            if s["running"] and self.stop == "tap_stop":
                boxes.append(_box("STOP", STOP_BUTTON))
            if s["running"] and s["counter"] is not None:
                boxes.append(_box(str(max(1, s["counter"] - s["tick"] % 100)), (SPIN[0], SPIN[1] - 60)))
        return Result.ok(boxes)

    def _state(self, path):
        return {"saldo": float(self.states[path]["balance"]), "posta": float(self.bet), "raw_texts": []}

    def _classify(self, slot, size, path, x, y):
        s = self.states[path]
        if s["running"]:
            return s["tick"] % 2 == 0                      # la freccia pulsa: un giro si vede come un fronte
        return not s["popup"]                              # fermo: pronto; con un popup il pulsante non e' pronto
