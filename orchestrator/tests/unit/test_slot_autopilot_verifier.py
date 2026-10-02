from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.verifier import (
    ActionVerifier,
    Expectation,
    classify_screen_state,
)
from orchestrator.tests.unit._fake_slot_device import (
    FakeSlotDevice,
    fake_ocr_reader,
    fake_screen_reader,
    fake_touch_injector,
)


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="ZT322ZSJVS", slot_name="pirots5", target_played_amount=Decimal("30.00"),
        frame_diff_poll_interval=0.01, frame_diff_max_wait_seconds=1.0,
        frame_diff_stable_threshold=0.02,
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def _verifier_for(device: FakeSlotDevice, config=None) -> ActionVerifier:
    return ActionVerifier(
        touch_injector=fake_touch_injector(device),
        screen_reader=fake_screen_reader(device),
        ocr_reader=fake_ocr_reader(device),
        config=config or _config(),
    )


class TestVerifiedTapHappyPaths:
    def test_bet_decrease_verified(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("bet_minus", 100, 100, lambda d: setattr(d, "bet", Decimal("0.50")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 100, 100, Expectation(kind="bet_decrease"))
        assert result.success
        outcome = result.data
        assert outcome.verified is True
        assert outcome.outcome == "success"
        assert outcome.bet_before == Decimal("1.0")
        assert outcome.bet_after == Decimal("0.5")
        assert outcome.tapped_coords == (100, 100)

    def test_bet_increase_verified_when_the_bet_goes_up(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.10"))
        device.add_element("bet_plus", 200, 200, lambda d: setattr(d, "bet", Decimal("0.20")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 200, 200, Expectation(kind="bet_increase"))

        assert result.data.verified is True and result.data.bet_after == Decimal("0.2")

    def test_bet_increase_not_verified_when_the_bet_does_not_move_or_goes_down(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("bet_minus", 100, 100, lambda d: setattr(d, "bet", Decimal("0.50")))
        verifier = _verifier_for(device)

        down = verifier.verified_tap("dev", 100, 100, Expectation(kind="bet_increase"))
        nothing = verifier.verified_tap("dev", 900, 900, Expectation(kind="bet_increase"))

        assert down.data.verified is False and nothing.data.verified is False

    def test_bet_increase_not_verified_when_the_bet_is_unreadable(self, tmp_path):
        device = FakeSlotDevice(tmp_path)
        ocr_reader = MagicMock()
        ocr_reader.read_slot_state.side_effect = [
            {"saldo": 10.0, "posta": None}, {"saldo": 10.0, "posta": None},
        ]
        verifier = ActionVerifier(fake_touch_injector(device), fake_screen_reader(device), ocr_reader, _config())

        result = verifier.verified_tap("dev", 10, 10, Expectation(kind="bet_increase"))

        assert result.data.verified is False

    def test_tap_on_dead_zone_is_no_effect(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("bet_minus", 100, 100, lambda d: setattr(d, "bet", Decimal("0.50")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 900, 900, Expectation(kind="bet_decrease"))
        assert result.success
        outcome = result.data
        assert outcome.verified is False
        assert outcome.outcome == "no_effect"

    def test_balance_decrease_at_exact_min_decrease_is_verified(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element(
            "cash_out", 200, 200, lambda d: setattr(d, "balance", d.balance - Decimal("0.10"))
        )
        verifier = _verifier_for(device)

        result = verifier.verified_tap(
            "dev", 200, 200, Expectation(kind="balance_decrease", min_decrease=Decimal("0.10")),
        )
        assert result.success
        assert result.data.verified is True

    def test_balance_decrease_below_min_decrease_is_not_verified(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element(
            "cash_out", 200, 200, lambda d: setattr(d, "balance", d.balance - Decimal("0.05"))
        )
        verifier = _verifier_for(device)

        result = verifier.verified_tap(
            "dev", 200, 200, Expectation(kind="balance_decrease", min_decrease=Decimal("0.10")),
        )
        assert result.success
        assert result.data.verified is False

    def test_bet_unchanged_confirms_min(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.10"))
        verifier = _verifier_for(device)

        result = verifier.verified_tap(
            "dev", 900, 900, Expectation(kind="bet_unchanged_confirms_min"),
        )
        assert result.success
        assert result.data.verified is True

    def test_tapped_coords_is_always_the_requested_coordinate(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 321, 654, Expectation(kind="bet_decrease"))
        assert result.success
        assert result.data.tapped_coords == (321, 654)

    def test_balance_changed_verifies_a_decrease(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.20"))
        device.add_element("spin", 540, 2000, lambda d: setattr(d, "balance", d.balance - Decimal("0.20")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))
        assert result.success
        assert result.data.verified is True

    def test_balance_changed_verifies_an_increase(self, tmp_path):
        """Una vincita che supera la puntata fa SALIRE il saldo: deve comunque
        contare come spin verificato, non come tap fallito."""
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.20"))
        device.add_element("spin", 540, 2000, lambda d: setattr(d, "balance", d.balance + Decimal("5.00")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))
        assert result.success
        assert result.data.verified is True

    def test_balance_changed_not_verified_when_truly_unchanged(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.20"))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 900, 900, Expectation(kind="balance_changed"))
        assert result.success
        assert result.data.verified is False

    def test_win_amount_populated_from_state_after_at_no_extra_cost(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.20"))

        def _spin_with_win(d):
            d.balance = d.balance + Decimal("0.25")
            d.win = Decimal("0.45")

        device.add_element("spin", 540, 2000, _spin_with_win)
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))
        assert result.success
        assert result.data.win_amount == Decimal("0.45")

    def test_state_after_is_exposed_so_callers_can_skip_a_new_ocr(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("0.20"))
        device.add_element("spin", 540, 2000, lambda d: setattr(d, "balance", d.balance - Decimal("0.20")))
        verifier = _verifier_for(device)

        result = verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))

        assert result.data.state_after is not None
        assert result.data.state_after["saldo"] == 9.80

    def test_win_amount_none_when_not_detected(self, tmp_path):
        device = FakeSlotDevice(tmp_path)
        screen_reader = fake_screen_reader(device)
        touch_injector = fake_touch_injector(device)
        ocr_reader = MagicMock()
        state_without_vincita = {"saldo": 9.80, "posta": 0.20, "spin_button_coords": (540, 2000)}
        ocr_reader.read_slot_state.side_effect = [
            {"saldo": 10.00, "posta": 0.20, "spin_button_coords": (540, 2000)},
            state_without_vincita,
        ]
        verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, _config())

        result = verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))
        assert result.success
        assert result.data.win_amount is None


class TestReuseOfCallerBeforeState:
    def test_provided_before_state_skips_the_before_screenshot_and_ocr(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("spin", 540, 2000, lambda d: setattr(d, "balance", Decimal("9.00")))
        screen_reader = fake_screen_reader(device)
        ocr_reader = MagicMock()
        ocr_reader.read_slot_state.return_value = {"saldo": 9.00, "posta": 1.00, "spin_button_coords": (540, 2000)}
        verifier = ActionVerifier(fake_touch_injector(device), screen_reader, ocr_reader, _config())
        before_state = {"saldo": 10.00, "posta": 1.00, "spin_button_coords": (540, 2000)}

        result = verifier.verified_tap(
            "dev", 540, 2000, Expectation(kind="balance_changed"),
            screenshot_before=device.screenshot(), state_before=before_state,
        )
        assert result.success
        assert result.data.verified is True
        assert result.data.balance_before == Decimal("10.0")
        assert result.data.balance_after == Decimal("9.0")
        ocr_reader.read_slot_state.assert_called_once()  # solo "after"

    def test_without_before_state_both_reads_still_happen(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        screen_reader = fake_screen_reader(device)
        ocr_reader = MagicMock()
        ocr_reader.read_slot_state.return_value = {"saldo": 10.00, "posta": 1.00, "spin_button_coords": (540, 2000)}
        verifier = ActionVerifier(fake_touch_injector(device), screen_reader, ocr_reader, _config())

        verifier.verified_tap("dev", 540, 2000, Expectation(kind="balance_changed"))
        assert ocr_reader.read_slot_state.call_count == 2


class TestVerifiedTapFailurePaths:
    def test_injector_failure_propagates_verbatim_without_second_screenshot(self, tmp_path):
        device = FakeSlotDevice(tmp_path)
        screen_reader = fake_screen_reader(device)
        touch_injector = MagicMock()
        touch_injector.tap_coordinates.return_value = Result.fail("adb tap failed", "TAP_MISSED")
        ocr_reader = fake_ocr_reader(device)
        verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, _config())

        result = verifier.verified_tap("dev", 100, 100, Expectation(kind="bet_decrease"))
        assert not result.success
        assert result.error_code == "TAP_MISSED"
        assert screen_reader._screenshot.call_count == 1  # solo lo screenshot "prima"

    def test_screenshot_before_failure_prevents_tap(self, tmp_path):
        screen_reader = MagicMock()
        screen_reader._screenshot.return_value = Result.fail("adb dead", "ADB_TIMEOUT")
        touch_injector = MagicMock()
        ocr_reader = MagicMock()
        verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, _config())

        result = verifier.verified_tap("dev", 100, 100, Expectation(kind="bet_decrease"))
        assert not result.success
        assert result.error_code == "ADB_TIMEOUT"
        touch_injector.tap_coordinates.assert_not_called()

    def test_ocr_exception_is_caught(self, tmp_path):
        device = FakeSlotDevice(tmp_path)
        screen_reader = fake_screen_reader(device)
        touch_injector = fake_touch_injector(device)
        ocr_reader = MagicMock()
        ocr_reader.read_slot_state.side_effect = RuntimeError("ocr exploded")
        verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, _config())

        result = verifier.verified_tap("dev", 100, 100, Expectation(kind="bet_decrease"))
        assert not result.success
        assert result.error_code == "VERIFICATION_ERROR"


class TestClassifyScreenState:
    def test_idle_when_spin_button_present(self):
        assert classify_screen_state({"spin_button_coords": (540, 2000)}) == "idle"

    def test_unsafe_when_spin_button_absent(self):
        assert classify_screen_state({"spin_button_coords": None}) == "unsafe"

    def test_idle_via_readable_saldo_and_posta_when_spin_button_is_icon_only(self):
        """Pirots5 e molte altre slot hanno un bottone spin puramente
        iconografico (una freccia circolare), mai un testo 'SPIN'/'GIRA'
        leggibile: senza questo fallback resterebbe sempre 'unsafe'."""
        ocr_state = {"spin_button_coords": None, "saldo": 5000.0, "posta": 0.20}
        assert classify_screen_state(ocr_state) == "idle"

    def test_unsafe_when_spin_button_absent_and_saldo_missing(self):
        ocr_state = {"spin_button_coords": None, "saldo": None, "posta": 0.20}
        assert classify_screen_state(ocr_state) == "unsafe"

    def test_unsafe_when_spin_button_absent_and_posta_missing(self):
        ocr_state = {"spin_button_coords": None, "saldo": 5000.0, "posta": None}
        assert classify_screen_state(ocr_state) == "unsafe"


class _RegionAwareOcr:
    """OCR finto con regione: nella fascia legge solo cio' che la fascia contiene."""

    def __init__(self, band_state, full_state):
        self.region = (0, 247, 1080, 2400)
        self.band_state, self.full_state = band_state, full_state
        self.regions_seen = []
        self.fail_inside_band = False

    def set_region(self, region):
        self.region = region

    def read_slot_state(self, path):
        self.regions_seen.append(self.region)
        if self.region == BAND and self.fail_inside_band:
            raise RuntimeError("ocr")
        return dict(self.band_state if self.region == BAND else self.full_state)


BAND = (0, 2145, 1080, 2255)
FULL = {"saldo": 10.0, "posta": 0.5, "raw_texts": ["full"]}


class TestRegionHint:
    def _verifier(self, tmp_path, ocr):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("bet_minus", 100, 100, lambda d: setattr(d, "bet", Decimal("0.50")))
        return ActionVerifier(fake_touch_injector(device), fake_screen_reader(device), ocr, _config())

    def test_the_after_reading_uses_the_band_and_the_region_is_restored(self, tmp_path):
        ocr = _RegionAwareOcr({"saldo": 10.0, "posta": 0.5}, FULL)
        verifier = self._verifier(tmp_path, ocr)
        before_path = verifier.screen_reader._screenshot("dev").data

        result = verifier.verified_tap(
            "dev", 100, 100, Expectation(kind="bet_decrease"),
            screenshot_before=before_path, state_before={"saldo": 10.0, "posta": 1.0}, region_hint=BAND,
        )

        assert result.data.verified and result.data.bet_after == Decimal("0.5")
        assert ocr.regions_seen == [BAND]                       # una sola lettura, rapida
        assert ocr.region == (0, 247, 1080, 2400)               # regione originale ripristinata
        assert result.data.state_after == {"saldo": 10.0, "posta": 0.5}

    def test_a_band_without_the_amounts_is_reread_on_the_full_screen(self, tmp_path):
        ocr = _RegionAwareOcr({"saldo": None, "posta": None}, FULL)
        verifier = self._verifier(tmp_path, ocr)

        result = verifier.verified_tap(
            "dev", 100, 100, Expectation(kind="bet_decrease"),
            screenshot_before=verifier.screen_reader._screenshot("dev").data,
            state_before={"saldo": 10.0, "posta": 1.0}, region_hint=BAND,
        )

        assert result.data.bet_after == Decimal("0.5")          # dalla lettura completa, non un falso "nessun effetto"
        assert ocr.regions_seen == [BAND, (0, 247, 1080, 2400)]
        assert ocr.region == (0, 247, 1080, 2400)

    def test_the_region_is_restored_even_when_the_reading_fails(self, tmp_path):
        ocr = _RegionAwareOcr({"saldo": 10.0, "posta": 0.5}, FULL)
        ocr.fail_inside_band = True
        verifier = self._verifier(tmp_path, ocr)

        result = verifier.verified_tap(
            "dev", 100, 100, Expectation(kind="bet_decrease"),
            screenshot_before=verifier.screen_reader._screenshot("dev").data,
            state_before={"saldo": 10.0, "posta": 1.0}, region_hint=BAND,
        )

        assert not result.success and result.error_code == "VERIFICATION_ERROR"
        assert ocr.region == (0, 247, 1080, 2400)

    def test_without_a_hint_the_reading_is_unchanged(self, tmp_path):
        ocr = _RegionAwareOcr({"saldo": 10.0, "posta": 0.5}, FULL)
        verifier = self._verifier(tmp_path, ocr)

        result = verifier.verified_tap(
            "dev", 100, 100, Expectation(kind="bet_decrease"),
            screenshot_before=verifier.screen_reader._screenshot("dev").data,
            state_before={"saldo": 10.0, "posta": 1.0},
        )

        assert ocr.regions_seen == [(0, 247, 1080, 2400)] and result.data.state_after == FULL


class TestTimingBreakdown:
    def test_the_outcome_line_reports_the_wait_and_the_reading_times(self, tmp_path, caplog):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("spin", 100, 100, lambda d: setattr(d, "balance", Decimal("9.00")))
        ocr = _RegionAwareOcr({"saldo": 9.0, "posta": 1.0}, FULL)
        verifier = ActionVerifier(fake_touch_injector(device), fake_screen_reader(device), ocr, _config())

        with caplog.at_level("INFO"):
            verifier.verified_tap(
                "dev", 100, 100, Expectation(kind="balance_changed"),
                screenshot_before=verifier.screen_reader._screenshot("dev").data,
                state_before={"saldo": 10.0, "posta": 1.0},
            )

        line = next(r.getMessage() for r in caplog.records if "esito=" in r.getMessage())
        assert "attesa schermo fermo" in line and "lettura" in line


class TestWaitUntilCalm:
    @staticmethod
    def _image(tmp_path, name, color):
        from PIL import Image
        path = tmp_path / name
        Image.new("RGB", (64, 64), color).save(path)
        return str(path)

    def _verifier(self, tmp_path, colors):
        """Un lettore finto che scatta schermate dei colori dati, nell'ordine (poi ripete l'ultimo)."""
        captures = {"n": 0}

        def screenshot(device_id):
            color = colors[min(captures["n"], len(colors) - 1)]
            captures["n"] += 1
            return Result.ok(self._image(tmp_path, f"cap_{captures['n']}.png", color))

        reader = MagicMock()
        reader._screenshot.side_effect = screenshot
        device = FakeSlotDevice(tmp_path)
        ocr = _RegionAwareOcr({"saldo": 10.0, "posta": 1.0}, FULL)
        return ActionVerifier(fake_touch_injector(device), reader, ocr, _config())

    def test_a_screen_equal_to_the_reference_is_calm_at_once(self, tmp_path):
        verifier = self._verifier(tmp_path, ["white"])
        reference = self._image(tmp_path, "ref.png", "white")

        result = verifier.wait_until_calm("dev", reference, 5.0)

        assert result.success and result.data.calm and not result.data.waited
        assert verifier.screen_reader._screenshot.call_count == 1       # UNO screenshot, nessun ciclo

    def test_a_changed_screen_that_then_settles_is_waited_for(self, tmp_path):
        verifier = self._verifier(tmp_path, ["black"])        # diverso dal riferimento, poi resta fermo
        reference = self._image(tmp_path, "ref.png", "white")

        result = verifier.wait_until_calm("dev", reference, 5.0)

        assert result.success and result.data.calm and result.data.waited

    def test_a_screen_that_never_settles_reports_it(self, tmp_path):
        verifier = self._verifier(tmp_path, ["black", "white"] * 40)       # cambia a ogni scatto
        reference = self._image(tmp_path, "ref.png", "gray")

        result = verifier.wait_until_calm("dev", reference, 0.5)

        assert result.success and not result.data.calm and result.data.waited

    def test_a_screenshot_failure_is_reported(self, tmp_path):
        verifier = self._verifier(tmp_path, ["white"])
        verifier.screen_reader._screenshot.side_effect = lambda device: Result.fail("adb down", "ADB_TIMEOUT")

        result = verifier.wait_until_calm("dev", self._image(tmp_path, "ref.png", "white"), 1.0)

        assert not result.success and result.error_code == "ADB_TIMEOUT"

    def test_the_outcome_line_reports_the_number_of_captures(self, tmp_path, caplog):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        device.add_element("spin", 100, 100, lambda d: setattr(d, "balance", Decimal("9.00")))
        ocr = _RegionAwareOcr({"saldo": 9.0, "posta": 1.0}, FULL)
        verifier = ActionVerifier(fake_touch_injector(device), fake_screen_reader(device), ocr, _config())

        with caplog.at_level("INFO"):
            verifier.verified_tap(
                "dev", 100, 100, Expectation(kind="balance_changed"),
                screenshot_before=verifier.screen_reader._screenshot("dev").data,
                state_before={"saldo": 10.0, "posta": 1.0},
            )

        line = next(r.getMessage() for r in caplog.records if "esito=" in r.getMessage())
        assert "scatti" in line
