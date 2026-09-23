import unittest
from unittest.mock import patch, MagicMock, call
from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.interface import (
    UIElement,
    ScreenContent,
    BetStatus,
)
from orchestrator.modules.touch_injector.injector import TouchInjector
from orchestrator.modules.touch_injector.interface import LayBetResult


DEVICE = "emulator-5554"


def _make_element(text="OK", bounds=None, resource_id=None):
    return UIElement(
        text=text,
        bounds=bounds or [100, 200, 300, 400],
        resource_id=resource_id,
        class_name=None,
        confidence=1.0,
    )


def _make_screen(elements=None, screen_type="unknown"):
    return ScreenContent(
        elements=elements or [],
        screen_type=screen_type,
        confidence=1.0,
        method="uiautomator",
        screenshot_path=None,
    )


class TestTapCoordinates(unittest.TestCase):
    """test_tap_coordinates_calls_adb + test_tap_coordinates_humanization"""

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_calls_adb(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.tap_coordinates(DEVICE, 500, 600)

        self.assertTrue(result.success)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:5], ["adb", "-s", DEVICE, "shell", "input"])
        self.assertEqual(cmd[5], "tap")

    @patch("orchestrator.modules.touch_injector.injector.random.uniform", return_value=0.5)
    @patch("orchestrator.modules.touch_injector.injector.random.randint", return_value=3)
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_humanization(self, mock_run, mock_sleep, mock_randint, mock_uniform):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.tap_coordinates(DEVICE, 500, 600)

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        # Con jitter +3, le coordinate devono essere 503, 603
        self.assertEqual(cmd[6], "503")
        self.assertEqual(cmd[7], "603")
        # Delay umanizzato deve essere stato chiamato
        mock_sleep.assert_called_once_with(0.5)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_coordinates_jitter_range(self, mock_run, mock_sleep):
        """Verifica che il jitter sia nell'intervallo ±5px."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        x_orig, y_orig = 500, 600
        for _ in range(50):
            injector.tap_coordinates(DEVICE, x_orig, y_orig)
            cmd = mock_run.call_args[0][0]
            tap_x = int(cmd[6])
            tap_y = int(cmd[7])
            self.assertGreaterEqual(tap_x, x_orig - 5)
            self.assertLessEqual(tap_x, x_orig + 5)
            self.assertGreaterEqual(tap_y, y_orig - 5)
            self.assertLessEqual(tap_y, y_orig + 5)


class TestScroll(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_down_uses_wm_size_and_swipes_bottom_to_top(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Physical size: 1080x1920", stderr=""),
            MagicMock(returncode=0, stderr=""),
        ]
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.scroll(DEVICE, "down")

        self.assertTrue(result.success)
        swipe_cmd = mock_run.call_args_list[1][0][0]
        self.assertEqual(swipe_cmd[:6], ["adb", "-s", DEVICE, "shell", "input", "swipe"])
        x1, y1, x2, y2 = swipe_cmd[6], swipe_cmd[7], swipe_cmd[8], swipe_cmd[9]
        self.assertEqual(x1, x2)
        self.assertGreater(int(y1), int(y2))  # down = swipe dal basso verso l'alto

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_up_swipes_top_to_bottom(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Physical size: 1080x1920", stderr=""),
            MagicMock(returncode=0, stderr=""),
        ]
        injector = TouchInjector(screen_reader=MagicMock())

        result = injector.scroll(DEVICE, "up")

        self.assertTrue(result.success)
        swipe_cmd = mock_run.call_args_list[1][0][0]
        y1, y2 = int(swipe_cmd[7]), int(swipe_cmd[9])
        self.assertLess(y1, y2)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_unparseable_wm_size_falls_back_to_default(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="error"),
            MagicMock(returncode=0, stderr=""),
        ]
        injector = TouchInjector(screen_reader=MagicMock())

        result = injector.scroll(DEVICE, "down")

        self.assertTrue(result.success)
        swipe_cmd = mock_run.call_args_list[1][0][0]
        # default 1080x1920 -> centro x = 540
        self.assertEqual(swipe_cmd[6], "540")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_caches_screen_size_across_calls(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Physical size: 1080x1920", stderr=""),
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=0, stderr=""),
        ]
        injector = TouchInjector(screen_reader=MagicMock())

        injector.scroll(DEVICE, "down")
        injector.scroll(DEVICE, "up")

        wm_size_calls = [c for c in mock_run.call_args_list if c[0][0][4:6] == ["wm", "size"]]
        self.assertEqual(len(wm_size_calls), 1)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_invalid_direction(self, mock_run, mock_sleep):
        injector = TouchInjector(screen_reader=MagicMock())

        result = injector.scroll(DEVICE, "diagonal")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_SCROLL_DIRECTION")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_scroll_swipe_failure(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="Physical size: 1080x1920", stderr=""),
            MagicMock(returncode=1, stderr="swipe failed"),
        ]
        injector = TouchInjector(screen_reader=MagicMock())

        result = injector.scroll(DEVICE, "down")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "SCROLL_FAILED")


class TestTapElement(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_element_success(self, mock_run, mock_sleep):
        """Mock ScreenReader.find_element → tap sul centro bounds [100,200,300,400] → centro (200,300).
        FIX-6: l'elemento sparisce dopo il tap -> verifica post-tap conferma il successo."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("Scommetti", bounds=[100, 200, 300, 400])
        reader.find_element.side_effect = [
            Result.ok(elem),
            Result.fail("gone", "NO_ELEMENTS_FOUND"),
        ]
        reader.read_screen.return_value = Result.ok(_make_screen())

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "Scommetti")

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[5], "tap")
        # Centro di [100,200,300,400] = (200,300) ± jitter 5px
        tap_x = int(cmd[6])
        tap_y = int(cmd[7])
        self.assertGreaterEqual(tap_x, 195)
        self.assertLessEqual(tap_x, 205)
        self.assertGreaterEqual(tap_y, 295)
        self.assertLessEqual(tap_y, 305)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_element_not_found(self, mock_run, mock_sleep):
        """find_element fallisce → TAP_MISSED dopo retry."""
        reader = MagicMock()
        reader.find_element.return_value = Result.fail("not found", "NO_ELEMENTS_FOUND")

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "invisible_button")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TAP_MISSED")
        # find_element chiamato 3 volte (1 + 2 retry)
        self.assertEqual(reader.find_element.call_count, 3)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_no_effect(self, mock_run, mock_sleep):
        """FIX-6: elemento ancora presente e schermo identico dopo il tap -> TAP_NO_EFFECT."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("Scommetti", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.return_value = Result.ok(_make_screen(elements=[elem]))

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "Scommetti")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TAP_NO_EFFECT")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_verify_failed(self, mock_run, mock_sleep):
        """FIX-6: la rilettura post-tap fallisce -> non possiamo verificare -> TAP_VERIFY_FAILED."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("Scommetti", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.side_effect = [
            Result.ok(_make_screen(elements=[elem])),
            Result.fail("device offline", "DEVICE_OFFLINE"),
        ]

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "Scommetti")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TAP_VERIFY_FAILED")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_tap_element_verify_false_skips_check(self, mock_run, mock_sleep):
        """FIX-6: verify=False -> nessuna rilettura post-tap, successo immediato."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("Scommetti", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)

        injector = TouchInjector(screen_reader=reader)
        result = injector.tap_element(DEVICE, "Scommetti", verify=False)

        self.assertTrue(result.success)
        reader.read_screen.assert_not_called()


class TestInputText(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_input_text_success(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)

        result = injector.input_text(DEVICE, "10.00")

        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[:5], ["adb", "-s", DEVICE, "shell", "input"])
        self.assertEqual(cmd[5], "text")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_input_text_with_field_hint(self, mock_run, mock_sleep):
        """Prima tap sul campo (via find_element), poi input text.
        FIX-6: il campo resta visibile dopo il focus, ma lo schermo cambia (es. tastiera) -> tap riuscito."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        reader = MagicMock()
        elem = _make_element("importo", bounds=[50, 100, 200, 150])
        reader.find_element.return_value = Result.ok(elem)
        keyboard_elem = _make_element("tastiera", bounds=[0, 400, 1080, 800])
        reader.read_screen.side_effect = [
            Result.ok(_make_screen(elements=[elem])),
            Result.ok(_make_screen(elements=[elem, keyboard_elem])),
        ]

        injector = TouchInjector(screen_reader=reader)
        result = injector.input_text(DEVICE, "25.00", field_hint="importo")

        self.assertTrue(result.success)
        # subprocess.run chiamato almeno 2 volte: tap + input text
        self.assertGreaterEqual(mock_run.call_count, 2)
        # L'ultimo call deve essere input text
        last_cmd = mock_run.call_args_list[-1][0][0]
        self.assertEqual(last_cmd[5], "text")


class TestPlaceLayBetVerifyThenAct(unittest.TestCase):
    """Tests per il pattern verify-then-act su ADB timeout durante place_lay_bet."""

    def _make_injector_with_timeout(self):
        """Crea un injector dove il tap di conferma lay alza TimeoutExpired."""
        reader = MagicMock()
        elem = _make_element("place lay", bounds=[100, 500, 300, 550])
        reader.find_element.return_value = Result.ok(elem)

        # FIX-6: read_screen deve restituire uno schermo diverso ad ogni chiamata, altrimenti
        # la verifica post-tap di tap_element (usata da input_text per il focus dei campi
        # odds/stake, prima del vero tap di conferma) rileverebbe "nessun effetto" e
        # romperebbe il flusso prima ancora di arrivare al tap di conferma che questi test
        # vogliono esercitare.
        screen_counter = {"n": 0}

        def _read_screen(*args, **kwargs):
            screen_counter["n"] += 1
            return Result.ok(_make_screen(elements=[_make_element(f"screen-{screen_counter['n']}")]))

        reader.read_screen.side_effect = _read_screen

        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=2)
        return injector, reader

    @staticmethod
    def _lay_side_effect_factory():
        """Side effect che fa passare i tap di focus campo e alza timeout solo
        sul tap di conferma (il 3° tap: focus odds, focus stake, confirm)."""
        state = {"tap_count": 0}

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[5] == "text":
                return MagicMock(returncode=0, stderr="")
            if cmd[5] == "tap":
                state["tap_count"] += 1
                if state["tap_count"] <= 2:
                    # Tap di focus per i campi odds e stake → ok
                    return MagicMock(returncode=0, stderr="")
                # 3° tap = conferma lay → timeout
                import subprocess as sp
                raise sp.TimeoutExpired(cmd, 10)
            return MagicMock(returncode=0, stderr="")

        return side_effect

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_verify_matched(self, mock_run, mock_sleep):
        """ADB timeout durante conferma → read_bet_status='matched' → successo."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="matched", lay_odds=2.10, confidence=1.0)
        )

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertTrue(result.success)
        self.assertTrue(result.data.confirmed)
        self.assertEqual(result.data.actual_odds, 2.10)
        reader.read_bet_status.assert_called()

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_verify_not_found(self, mock_run, mock_sleep):
        """ADB timeout → read_bet_status='not_found' → LAY_NOT_PLACED_RETRY_SAFE."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="not_found", lay_odds=None, confidence=1.0)
        )

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "LAY_NOT_PLACED_RETRY_SAFE")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_ambiguous_state(self, mock_run, mock_sleep):
        """ADB timeout → read_bet_status fallisce → AMBIGUOUS_STATE."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.fail("screen unreadable", "OCR_LOW_CONFIDENCE")

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "AMBIGUOUS_STATE")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_place_lay_bet_verify_unknown_status(self, mock_run, mock_sleep):
        """FIX-5: ADB timeout → read_bet_status='unknown' (schermata non riconosciuta) → AMBIGUOUS_STATE, mai confirmed."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="unknown", lay_odds=None, confidence=1.0)
        )

        result = injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "AMBIGUOUS_STATE")

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_verify_lay_state_passes_force_refresh(self, mock_run, mock_sleep):
        """FIX-22: _verify_lay_state deve bypassare la cache schermo dopo un
        timeout ADB, altrimenti rischia di leggere uno stato stantio."""
        injector, reader = self._make_injector_with_timeout()
        mock_run.side_effect = self._lay_side_effect_factory()

        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="matched", lay_odds=2.10, confidence=1.0)
        )

        injector.place_lay_bet(DEVICE, "match_odds", 2.10, 50.0)

        reader.read_bet_status.assert_called_with(DEVICE, betfair=True, force_refresh=True)


class TestWaitLayConfirmation(unittest.TestCase):
    """Test diretti su _wait_lay_confirmation (percorso normale, tap di conferma riuscito)."""

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_wait_lay_confirmation_unknown_status(self, mock_sleep):
        """FIX-5: status='unknown' per 3 letture consecutive durante il polling -> AMBIGUOUS_STATE,
        non BET_NOT_CONFIRMED e mai confirmed=True. Una singola lettura 'unknown' isolata non
        deve invece far fallire subito (si tollera fino a 2 letture ambigue consecutive)."""
        reader = MagicMock()
        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="unknown", lay_odds=None, confidence=1.0)
        )
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=15)

        result = injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "AMBIGUOUS_STATE")
        # Deve aver atteso 3 letture "unknown" consecutive prima di escalare
        self.assertEqual(reader.read_bet_status.call_count, 3)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_wait_lay_confirmation_unknown_streak_resets(self, mock_sleep):
        """FIX-5: 2 letture 'unknown' seguite da una lettura riconosciuta non devono
        far scattare l'escalation — lo streak di ambiguita' si resetta."""
        reader = MagicMock()
        reader.read_bet_status.side_effect = [
            Result.ok(BetStatus(status="unknown", lay_odds=None, confidence=1.0)),
            Result.ok(BetStatus(status="unknown", lay_odds=None, confidence=1.0)),
            Result.ok(BetStatus(status="matched", lay_odds=2.10, confidence=1.0)),
        ]
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=15)

        result = injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        self.assertTrue(result.success)
        self.assertTrue(result.data.confirmed)
        self.assertEqual(reader.read_bet_status.call_count, 3)

    @patch("orchestrator.modules.touch_injector.injector.time.monotonic")
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_wait_lay_confirmation_respects_timeout_despite_slow_read(self, mock_sleep, mock_monotonic):
        """FIX-21: anche se read_bet_status è lenta, il deadline (wall-clock via monotonic)
        viene rispettato — non si continua a fare polling oltre bet_confirm_timeout."""
        # start=0; check inizio 1a iterazione: remaining=15-0=15>0, prima iterazione -> parte
        # sempre; dopo la read "lenta" elapsed=16-0=16 (> timeout, logga warning); check
        # inizio 2a iterazione: remaining=15-16=-1<=0 -> esce
        mock_monotonic.side_effect = [0, 0, 16, 16]

        reader = MagicMock()
        reader.read_bet_status.return_value = Result.fail("slow/ambiguous read", "OCR_LOW_CONFIDENCE")
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=15)

        result = injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BET_NOT_CONFIRMED")
        # Una sola iterazione eseguita, nonostante bet_confirm_timeout=15 permetterebbe
        # normalmente ~15 cicli da 1s se il tempo non fosse stato consumato dalla read lenta
        self.assertEqual(reader.read_bet_status.call_count, 1)

    @patch("orchestrator.modules.touch_injector.injector.time.monotonic")
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_bet_confirm_wall_clock(self, mock_sleep, mock_monotonic):
        """FIX-21: se una singola lettura supera il bet_confirm_timeout dichiarato, viene
        loggato un warning e non parte un'ulteriore iterazione di polling."""
        mock_monotonic.side_effect = [0, 0, 16, 16]

        reader = MagicMock()
        reader.read_bet_status.return_value = Result.fail("slow read", "OCR_LOW_CONFIDENCE")
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=15)

        with self.assertLogs("orchestrator.modules.touch_injector.injector", level="WARNING") as cm:
            result = injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BET_NOT_CONFIRMED")
        self.assertTrue(any("bet_confirm_timeout" in msg for msg in cm.output))
        self.assertEqual(reader.read_bet_status.call_count, 1)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_wait_lay_confirmation_margin_guard_stops_before_deadline(self, mock_sleep):
        """FIX-21: non si avvia una nuova iterazione (oltre la prima) se il tempo rimanente
        e' inferiore ad adb_timeout — evita di iniziare una lettura che rischia di sforare."""
        reader = MagicMock()
        reader.read_bet_status.return_value = Result.fail("still loading", "OCR_LOW_CONFIDENCE")
        # bet_confirm_timeout appena sopra 1s: dopo la prima iterazione (~1s) il tempo
        # rimanente e' gia' sotto adb_timeout (10s di default) -> niente seconda iterazione
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=1.5)

        result = injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "BET_NOT_CONFIRMED")
        # Garantito almeno un tentativo, ma non un secondo dato il margine insufficiente
        self.assertEqual(reader.read_bet_status.call_count, 1)

    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    def test_wait_lay_confirmation_passes_force_refresh(self, mock_sleep):
        """FIX-22: il polling deve bypassare la cache a 5s dello ScreenReader,
        altrimenti in un loop da 1s vedrebbe sempre lo stesso screenshot stantio."""
        reader = MagicMock()
        reader.read_bet_status.return_value = Result.ok(
            BetStatus(status="matched", lay_odds=2.10, confidence=1.0)
        )
        injector = TouchInjector(screen_reader=reader, bet_confirm_timeout=15)

        injector._wait_lay_confirmation(DEVICE, 2.10, 50.0)

        reader.read_bet_status.assert_called_with(DEVICE, betfair=True, force_refresh=True)


class TestNavigateTo(unittest.TestCase):
    """FIX-23: navigate_to deve tenere conto dell'esito reale del tap, non ignorarlo."""

    def test_navigate_to_reaches_destination_immediately(self):
        reader = MagicMock()
        reader.read_screen.return_value = Result.ok(_make_screen(screen_type="balance_page"))

        injector = TouchInjector(screen_reader=reader)
        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertTrue(result.success)

    def test_navigate_to_tap_failure(self):
        """_human_tap fallisce sempre sullo stesso elemento -> FIX-23 interrompe dopo 2
        fallimenti consecutivi con NAVIGATION_FAILED, senza attendere max_steps."""
        reader = MagicMock()
        reader.read_screen.return_value = Result.ok(_make_screen(screen_type="unknown"))
        elem = _make_element("saldo", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)

        injector = TouchInjector(screen_reader=reader)
        injector._human_tap = MagicMock(return_value=Result.fail("tap missed", "TAP_MISSED"))

        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NAVIGATION_FAILED")
        self.assertEqual(result.error, "Navigation tap failed")
        # Si ferma dopo il 2° fallimento consecutivo, non prosegue fino a max_steps
        self.assertEqual(injector._human_tap.call_count, 2)

    def test_navigate_tap_failed(self):
        """FIX-23: 2 tap falliti di seguito sullo stesso elemento -> NAVIGATION_FAILED
        immediato ('Navigation tap failed'), il tap non va mai considerato riuscito."""
        reader = MagicMock()
        reader.read_screen.return_value = Result.ok(_make_screen(screen_type="unknown"))
        elem = _make_element("saldo", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)

        injector = TouchInjector(screen_reader=reader)
        injector._human_tap = MagicMock(return_value=Result.fail("tap missed", "TAP_MISSED"))

        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NAVIGATION_FAILED")
        self.assertEqual(result.error, "Navigation tap failed")

    def test_navigate_to_recovers_after_single_tap_failure(self):
        """FIX-23: un singolo fallimento seguito da un successo sullo stesso elemento
        non deve far scattare l'interruzione anticipata (serve la ripetizione consecutiva)."""
        reader = MagicMock()
        elem = _make_element("saldo", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)
        reader.read_screen.side_effect = [
            Result.ok(_make_screen(screen_type="unknown")),
            Result.ok(_make_screen(screen_type="unknown")),
            Result.ok(_make_screen(screen_type="balance_page")),
        ]

        injector = TouchInjector(screen_reader=reader)
        injector._human_tap = MagicMock(side_effect=[
            Result.fail("tap missed", "TAP_MISSED"),
            Result.ok(True),
        ])

        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertTrue(result.success)

    def test_navigate_to_no_navigation_path(self):
        """Nessun hint trovato sullo schermo -> fallimento immediato al primo step."""
        reader = MagicMock()
        reader.read_screen.return_value = Result.ok(_make_screen(screen_type="unknown"))
        reader.find_element.return_value = Result.fail("not found", "NO_ELEMENTS_FOUND")

        injector = TouchInjector(screen_reader=reader)
        injector._human_tap = MagicMock(return_value=Result.ok(True))

        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NAVIGATION_FAILED")
        self.assertIn("No navigation path found", result.error)

    def test_navigate_to_destination_not_reached_after_successful_taps(self):
        """I tap hanno sempre successo ma la destinazione non viene mai raggiunta entro
        max_steps -> messaggio distinto da quello per 'nessun tap riuscito'."""
        reader = MagicMock()
        reader.read_screen.return_value = Result.ok(_make_screen(screen_type="unknown"))
        elem = _make_element("saldo", bounds=[100, 200, 300, 400])
        reader.find_element.return_value = Result.ok(elem)

        injector = TouchInjector(screen_reader=reader)
        injector._human_tap = MagicMock(return_value=Result.ok(True))

        result = injector.navigate_to(DEVICE, "balance_page", "Stanleybet")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NAVIGATION_FAILED")
        self.assertIn("not reached after", result.error)


class TestHumanTapDelay(unittest.TestCase):

    @patch("orchestrator.modules.touch_injector.injector.random.uniform")
    @patch("orchestrator.modules.touch_injector.injector.time.sleep")
    @patch("orchestrator.modules.touch_injector.injector.subprocess.run")
    def test_human_tap_random_delay(self, mock_run, mock_sleep, mock_uniform):
        """Verifica che time.sleep sia chiamato con valore da random.uniform(0.3, 1.5)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mock_uniform.return_value = 0.87

        reader = MagicMock()
        injector = TouchInjector(screen_reader=reader)
        injector._human_tap(DEVICE, 100, 200)

        mock_uniform.assert_called_once_with(0.3, 1.5)
        mock_sleep.assert_called_once_with(0.87)


if __name__ == "__main__":
    unittest.main()
