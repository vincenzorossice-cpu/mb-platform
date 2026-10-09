import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot import precise_hitl
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.precise_hitl import handle_precise_hitl_escalation
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.strategies.hitl_strategy import HITLStrategy
from orchestrator.modules.slot_autopilot.supervisor import Assist, SlotSupervisor
from orchestrator.modules.slot_ocr.reader import OCRBox
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.supervisor.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_menu.time.sleep", lambda s: None)
    monkeypatch.setattr("orchestrator.modules.slot_ocr.vision_reader.time.sleep", lambda s: None)


@pytest.fixture
def shot(tmp_path):
    path = tmp_path / "s.png"
    Image.new("RGB", (1080, 2400), "white").save(path)
    return str(path)


@pytest.fixture
def changed_shot(tmp_path):
    path = tmp_path / "c.png"
    Image.effect_noise((1080, 2400), 90).save(path)
    return str(path)


def _gpt(payload):
    gpt = MagicMock()
    gpt._VISION_MAX_TOKENS = 300
    replies = payload if isinstance(payload, list) else [payload]
    gpt._create_completion.side_effect = [Result.ok(reply) for reply in replies]
    return gpt


def _supervisor(shot_path, payload, describe=None, touch=None, **kwargs):
    reader = MagicMock()
    reader._screenshot.return_value = Result.ok(shot_path)
    return SlotSupervisor(_gpt(payload), reader, touch or MagicMock(), describe=describe, **kwargs), reader


IDLE_AT_MIN = {"screen_state": "idle", "credit_text": "50 000.00", "bet_text": "0.10", "action": "none",
               "reasoning": "ok"}


class TestSupervisorReading:
    def test_reads_bet_and_credit_and_logs_the_purpose(self, shot):
        supervisor, _ = _supervisor(shot, IDLE_AT_MIN)

        observation = supervisor.observe(shot)

        assert observation.bet == Decimal("0.1") and observation.credit == Decimal("50000.0")
        assert supervisor.gpt_client._create_completion.call_args.kwargs["purpose"] == "supervisore"
        assert "jackpot" in supervisor.gpt_client._create_completion.call_args.args[0][0]["content"].lower()

    def test_a_bet_that_is_not_below_the_credit_is_dropped(self, shot):
        supervisor, _ = _supervisor(shot, dict(IDLE_AT_MIN, bet_text="50 000.00"))

        assert supervisor.observe(shot).bet is None

    def test_the_call_budget_is_enforced(self, shot):
        supervisor, _ = _supervisor(shot, [IDLE_AT_MIN] * 3, max_calls=2)

        assert supervisor.observe(shot) is not None and supervisor.observe(shot) is not None
        assert supervisor.observe(shot) is None

    def test_an_unusable_answer_gives_none(self, shot):
        supervisor, _ = _supervisor(shot, IDLE_AT_MIN)
        supervisor.gpt_client._create_completion.side_effect = [Result.fail("boom", "X")]

        assert supervisor.observe(shot) is None


class TestAssist:
    def test_the_bet_already_at_the_known_minimum_is_confirmed_without_touching(self, shot):
        touch = MagicMock()
        supervisor, _ = _supervisor(shot, IDLE_AT_MIN, touch=touch)

        verdict = supervisor.assist("dev", Decimal("0.10"))

        assert verdict == Assist("at_min", Decimal("0.1"))
        touch.tap_coordinates.assert_not_called()

    def test_a_different_bet_is_not_at_min(self, shot):
        supervisor, _ = _supervisor(shot, dict(IDLE_AT_MIN, bet_text="0.50"))

        assert supervisor.assist("dev", Decimal("0.10")).outcome == "nothing"

    def test_an_unwanted_menu_is_closed_with_the_x_found_by_the_locator(self, shot, changed_shot):
        touch = MagicMock()
        describe = MagicMock(return_value=(1016, 625))
        payload = {"screen_state": "modal", "credit_text": None, "bet_text": None, "action": "close",
                   "target_description": "the X at the top right of the panel", "reasoning": "menu"}
        supervisor, reader = _supervisor(shot, payload, describe=describe, touch=touch)
        reader._screenshot.side_effect = [Result.ok(shot), Result.ok(changed_shot)]

        verdict = supervisor.assist("dev", Decimal("0.10"))

        assert verdict.outcome == "closed"
        touch.tap_coordinates.assert_called_once_with("dev", 1016, 625)

    def test_a_close_that_changes_nothing_is_not_reported_as_closed(self, shot):
        payload = {"screen_state": "modal", "credit_text": None, "bet_text": None, "action": "close",
                   "target_description": "the X", "reasoning": "menu"}
        supervisor, _ = _supervisor(shot, payload, describe=MagicMock(return_value=(10, 10)))

        assert supervisor.assist("dev", Decimal("0.10")).outcome == "nothing"

    def test_an_imprecise_point_is_never_tapped(self, shot):
        touch = MagicMock()
        payload = {"screen_state": "modal", "action": "close", "target_description": "the X", "reasoning": ""}
        supervisor, _ = _supervisor(shot, payload, describe=MagicMock(return_value=None), touch=touch)

        assert supervisor.assist("dev", None).outcome == "nothing"
        touch.tap_coordinates.assert_not_called()

    def test_it_never_taps_anything_that_looks_like_a_spin(self, shot):
        touch = MagicMock()
        payload = {"screen_state": "modal", "action": "close", "target_description": "the big spin button",
                   "reasoning": ""}
        supervisor, _ = _supervisor(shot, payload, describe=MagicMock(return_value=(5, 5)), touch=touch)

        assert supervisor.assist("dev", None).outcome == "nothing"
        touch.tap_coordinates.assert_not_called()

    def test_interventions_are_capped(self, shot):
        supervisor, _ = _supervisor(shot, [IDLE_AT_MIN] * 5, max_interventions=1)

        assert supervisor.assist("dev", Decimal("0.10")).outcome == "at_min"
        assert supervisor.assist("dev", Decimal("0.10")).outcome == "nothing"


def _box(text, x, y):
    return OCRBox(text=text, bbox=(x - 30, y - 15, x + 30, y + 15), confidence=0.9)


MENU = [_box("0,10", 200, 1000), _box("0,20", 400, 1000), _box("0,50", 600, 1000),
        _box("1,00", 200, 1200), _box("2,00", 400, 1200)]


def _ctx(boxes, posta=None):
    return LocateContext(device_id="dev", slot_name="s", screenshot_path="/tmp/x.png", screen_width=1080,
                         screen_height=2400, text_boxes=boxes, slot_ocr_state={"posta": posta} if posta else {})


def _ok_tap():
    from orchestrator.modules.slot_autopilot.verifier import TapOutcome
    return Result.ok(TapOutcome(
        tapped_coords=(0, 0), verified=True, screenshot_before="a", screenshot_after="b", diff_score=0.3,
        balance_before=None, balance_after=None, bet_before=None, bet_after=None,
        screen_state_before="idle", screen_state_after="idle", outcome="success"))


class TestMenuSelectionWithSupervisor:
    def _selector(self, supervisor):
        locator = MagicMock()
        locator.locate.return_value = Result.ok(LocateCandidate(x=540, y=2088, confidence=0.9, strategy_name="memory"))
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.verified_tap.return_value = _ok_tap()
        return BetMenuSelector(locator, verifier, supervisor=supervisor)

    def test_a_jackpot_misread_after_the_selection_is_overruled_by_the_supervisor(self):
        supervisor = MagicMock()
        supervisor.read_bet.return_value = Decimal("0.10")
        selector = self._selector(supervisor)
        provider = MagicMock(side_effect=[Result.ok(_ctx(MENU)), Result.ok(_ctx([], posta=7181009.38)),
                                         Result.ok(_ctx([], posta=7181009.38))])

        result = selector.select_lowest("dev", "s", _ctx([]), provider)

        assert result.success and result.data.final_bet == Decimal("0.10")

    def test_without_a_supervisor_the_misread_still_fails(self):
        selector = self._selector(None)
        provider = MagicMock(side_effect=[Result.ok(_ctx(MENU)), Result.ok(_ctx([], posta=7181009.38)),
                                          Result.ok(_ctx([], posta=7181009.38))] * 3)

        assert selector.select_lowest("dev", "s", _ctx([]), provider).error_code == "MENU_SELECTION_NOT_CONFIRMED"

    def test_a_supervisor_that_really_sees_another_bet_confirms_the_failure(self):
        supervisor = MagicMock()
        supervisor.read_bet.return_value = Decimal("0.50")
        selector = self._selector(supervisor)
        provider = MagicMock(side_effect=[Result.ok(_ctx(MENU)), Result.ok(_ctx([], posta=7181009.38)),
                                          Result.ok(_ctx([], posta=7181009.38))] * 3)

        assert selector.select_lowest("dev", "s", _ctx([]), provider).error_code == "MENU_SELECTION_NOT_CONFIRMED"


def _config():
    return AutoPilotConfig(
        device_id="dev", slot_name="s", target_played_amount=Decimal("30"), max_bet_lower_attempts=5,
        frame_diff_poll_interval=0.01, frame_diff_max_wait_seconds=1.0, bet_burst_taps=1,
        bet_burst_interval_seconds=0.0, floor_probe_enabled=False, avoid_spin_zone=False,
    )


def _profile(min_bet=0.1):
    return SimpleNamespace(verified=True, known_min_bet=min_bet, elements={}, canvas_bbox=None)


def _bet_context(bet=50.0, profile=None):
    return LocateContext(device_id="dev", slot_name="s", screenshot_path="/tmp/x.png", screen_width=1080,
                         screen_height=2400, slot_ocr_state={"posta": bet}, profile=profile)


def _controller(locator, supervisor):
    step_logger = MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    locator.is_chain_exhausted.return_value = False
    locator.is_trusted_strategy.return_value = False
    locator.last_hitl_tap.return_value = None
    locator._STRATEGY_ORDER = ("profile", "memory", "ocr_anchor", "spatial_heuristic", "template", "gpt", "hitl")
    return BetController(locator, MagicMock(), step_logger, _config(), supervisor=supervisor)


class TestBetControllerWithSupervisor:
    def test_the_supervisor_runs_before_the_operator_and_confirms_the_minimum(self):
        locator = MagicMock()
        locator.locate.return_value = Result.fail("none", "ELEMENT_NOT_LOCATABLE")
        supervisor = MagicMock()
        supervisor.assist.return_value = Assist("at_min", Decimal("0.10"))
        controller = _controller(locator, supervisor)

        result = controller.set_min_bet("dev", "s", "ep", MagicMock(return_value=Result.ok(_bet_context(profile=_profile()))))

        assert result.data.min_bet_verified and "supervisor" in result.data.strategy_sequence
        assert result.data.final_bet == Decimal("0.10")
        # la prima ricerca esclude l'operatore, e non ce ne sono altre
        assert locator.locate.call_args_list[0].kwargs["exclude_strategies"] == ("hitl",)
        assert len(locator.locate.call_args_list) == 1

    def test_when_the_supervisor_has_nothing_the_operator_is_reached_with_only_hitl(self):
        locator = MagicMock()
        locator.locate.return_value = Result.fail("none", "HITL_STOP_REQUESTED")
        supervisor = MagicMock()
        supervisor.assist.return_value = Assist("nothing")
        controller = _controller(locator, supervisor)

        result = controller.set_min_bet("dev", "s", "ep", MagicMock(return_value=Result.ok(_bet_context(profile=_profile()))))

        assert result.data.aborted_reason == "HITL_STOP"
        calls = locator.locate.call_args_list
        assert calls[0].kwargs["exclude_strategies"] == ("hitl",)
        assert calls[1].kwargs["exclude_strategies"] == ("profile", "memory", "ocr_anchor", "spatial_heuristic",
                                                         "template", "gpt")

    def test_an_operator_statement_confirms_the_minimum_without_any_tap(self):
        locator = MagicMock()
        locator.locate.side_effect = [Result.fail("x", "ELEMENT_NOT_LOCATABLE"),
                                      Result.fail("statement", "HITL_INFO_ONLY")]
        supervisor = MagicMock()
        supervisor.assist.return_value = Assist("nothing")
        supervisor.read_bet.return_value = Decimal("0.10")
        controller = _controller(locator, supervisor)

        result = controller.set_min_bet("dev", "s", "ep", MagicMock(return_value=Result.ok(_bet_context(profile=_profile()))))

        assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.10")
        assert "hitl" in result.data.strategy_sequence
        controller.verifier.verified_tap.assert_not_called()

    def test_without_a_supervisor_the_locate_is_the_historical_one(self):
        locator = MagicMock()
        locator.locate.return_value = Result.fail("stop", "HITL_STOP_REQUESTED")
        controller = _controller(locator, None)

        controller.set_min_bet("dev", "s", "ep", MagicMock(return_value=Result.ok(_bet_context())))

        assert "exclude_strategies" not in locator.locate.call_args.kwargs


class TestHitlStatements:
    def _run(self, monkeypatch, shot, text):
        monkeypatch.setattr(precise_hitl, "ask_human_help", MagicMock(side_effect=[("text", text), ("stop",)]))
        monkeypatch.setattr(precise_hitl, "read_ocr_state", MagicMock(return_value={"posta": 0.1, "saldo": 5.0, "raw_texts": []}))
        monkeypatch.setattr(precise_hitl.time, "sleep", lambda s: None)
        reader = MagicMock()
        reader._screenshot.return_value = Result.ok(shot)
        ocr_reader = MagicMock()
        ocr_reader.read_slot_state.return_value = {"posta": 1.0, "saldo": 5.0, "raw_texts": []}
        touch = MagicMock()
        describe = MagicMock(return_value=(152, 525))
        result = handle_precise_hitl_escalation(reader, ocr_reader, touch, "dev", None, 0.1, describe)
        return result, touch, describe

    def test_a_state_sentence_taps_nothing_and_is_reported_as_information(self, monkeypatch, shot):
        result, touch, describe = self._run(
            monkeypatch, shot, "sei gia nel menu puntata ed è gia selezionata la puntata minima")

        assert result[0] == "info"
        touch.tap_coordinates.assert_not_called()
        describe.assert_not_called()

    def test_a_sentence_with_an_instruction_is_still_executed(self, monkeypatch, shot):
        monkeypatch.setattr(precise_hitl, "read_ocr_state",
                            MagicMock(return_value={"posta": 0.1, "saldo": 5.0, "raw_texts": []}))
        result, touch, _ = self._run(
            monkeypatch, shot, "la puntata è gia al minimo ma hai aperto un menu indesiderato per uscire premi la X")

        touch.tap_coordinates.assert_called_once()

    def test_the_hitl_strategy_turns_the_statement_into_an_info_failure(self, monkeypatch):
        monkeypatch.setattr(
            "orchestrator.modules.slot_autopilot.strategies.hitl_strategy.handle_precise_hitl_escalation",
            lambda *args: ("info", "gia al minimo", None, None))
        strategy = HITLStrategy(MagicMock(), MagicMock(), MagicMock(), describer=MagicMock())
        context = LocateContext(device_id="dev", slot_name="s", screenshot_path="/x.png", screen_width=1, screen_height=1)

        result = strategy.locate("bet_minus", context)

        assert result.error_code == "HITL_INFO_ONLY" and strategy.interventions == 1


def _ocr(states):
    ocr = MagicMock()
    ocr.region = None
    ocr.read_slot_state.side_effect = list(states)
    return ocr


def _vision(*reads):
    vision = MagicMock()
    vision.read.side_effect = [dict(read) for read in reads]
    return vision


class TestJackpotCounters:
    def test_a_jackpot_misread_by_the_vision_is_not_used_as_balance(self):
        # OCR: saldo 50000 giusto; visione: contatore jackpot che a una seconda lettura e' cambiato.
        vision = _vision({"saldo": 22522769.14, "posta": 50000.0}, {"saldo": 22522772.49, "posta": 50000.0})
        reader = ResilientOCRReader(
            _ocr([{"saldo": 50000.0, "posta": None, "raw_texts": []}]), vision,
            capture=lambda: "/second.png", confirm_delay=0,
        )

        state = reader.read_slot_state("/first.png")

        assert state["saldo"] == 50000.0            # il jackpot non e' un ancoraggio
        assert state["posta"] is None               # 50000 e' il credito letto come puntata: non inferiore al saldo
        assert reader._trusted["saldo"] == 50000.0

    def test_a_value_that_is_identical_in_both_reads_is_believed(self):
        vision = _vision({"saldo": 3000.0, "posta": 1.0}, {"saldo": 3000.0, "posta": 1.0})
        reader = ResilientOCRReader(
            _ocr([{"saldo": 1500.0, "posta": None, "raw_texts": []}]), vision,
            capture=lambda: "/second.png", confirm_delay=0,
        )

        # OCR 1500 contro visione 3000 (non potenza di dieci): la visione e' uguale in due letture, quindi vince
        assert reader.read_slot_state("/first.png")["saldo"] == 3000.0
        assert vision.read.call_count == 2

    def test_a_ticking_bet_is_not_accepted(self):
        vision = _vision({"saldo": 50000.0, "posta": 7181009.38}, {"saldo": 50000.0, "posta": 7181045.77})
        reader = ResilientOCRReader(
            _ocr([{"saldo": 50000.0, "posta": None, "raw_texts": []}]), vision,
            capture=lambda: "/second.png", confirm_delay=0,
        )

        state = reader.read_slot_state("/first.png")

        assert state["posta"] is None

    def test_a_bet_equal_to_the_balance_is_dropped(self):
        reader = ResilientOCRReader(
            _ocr([{"saldo": None, "posta": None, "raw_texts": []}]), _vision({"saldo": 50000.0, "posta": 50000.0}),
        )

        state = reader.read_slot_state("/first.png")

        assert state["saldo"] == 50000.0 and state["posta"] is None

    def test_without_a_capture_function_the_old_behaviour_is_unchanged(self):
        vision = _vision({"saldo": 50000.0, "posta": 1000.0})
        reader = ResilientOCRReader(_ocr([{"saldo": 50000.0, "posta": None, "raw_texts": []}]), vision)

        assert reader.read_slot_state("/a.png")["posta"] == 1000.0

    def test_repeated_warnings_are_throttled(self, caplog):
        reader = ResilientOCRReader(_ocr([]), _vision())
        with caplog.at_level("WARNING"):
            for _ in range(25):
                reader._warn("k", "messaggio ripetuto %s", 1)

        assert len([r for r in caplog.records if "messaggio ripetuto" in r.getMessage()]) == 3


class TestUnstableVisionDoesNotJoinTheSpinLoop:
    def test_pirots5_session_of_the_7th_ocr_reads_the_bet_fine_and_the_vision_misreads_it_once(self):
        # 12:39: OCR saldo 4995.78 e puntata 0.2; la visione legge la puntata 2.0, poi (seconda lettura) 0.2.
        vision = _vision({"saldo": 4995.78, "posta": 2.0}, {"saldo": 4995.78, "posta": 0.2})
        ocr = _ocr([{"saldo": 4995.78, "posta": 0.2, "raw_texts": []}] + [
            {"saldo": 4995.58 - 0.2 * i, "posta": 0.2, "raw_texts": []} for i in range(5)])
        reader = ResilientOCRReader(ocr, vision, capture=lambda: "/second.png", confirm_delay=0)

        first = reader.read_slot_state("/first.png")
        later = [reader.read_slot_state(f"/spin{i}.png") for i in range(5)]

        assert first["posta"] == 0.2 and all(state["posta"] == 0.2 for state in later)
        assert not reader.prefer_vision                       # la visione non e' piu' un campo del ciclo
        assert vision.read.call_count == 2                    # solo prima lettura + conferma, nessuna chiamata per spin

    def test_a_vision_error_never_discards_an_ocr_amount_when_the_ocr_drives_the_field(self):
        vision = _vision({"saldo": 100.0, "posta": 2.0}, {"saldo": 100.0, "posta": 0.2})
        ocr = _ocr([{"saldo": 100.0, "posta": 0.2, "raw_texts": []}, {"saldo": 99.8, "posta": 0.2, "raw_texts": []}])
        reader = ResilientOCRReader(ocr, vision, capture=lambda: "/second.png", confirm_delay=0)
        reader.read_slot_state("/first.png")
        vision.read.side_effect = AssertionError("la visione non deve essere chiamata")

        assert reader.read_slot_state("/spin1.png")["posta"] == 0.2

    def test_with_an_empty_ocr_the_unstable_vision_still_stays_in_play_but_unconfirmed(self):
        vision = _vision({"saldo": 50000.0, "posta": 7181009.38}, {"saldo": 50000.0, "posta": 7181045.77})
        reader = ResilientOCRReader(
            _ocr([{"saldo": 50000.0, "posta": None, "raw_texts": []}]), vision,
            capture=lambda: "/second.png", confirm_delay=0,
        )

        state = reader.read_slot_state("/first.png")

        assert state["posta"] is None and reader.prefer_vision
