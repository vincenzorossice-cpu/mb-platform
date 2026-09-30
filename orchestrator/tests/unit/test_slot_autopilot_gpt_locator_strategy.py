import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from orchestrator.core.result import Result
from orchestrator.modules.slot_autopilot.strategies.base import LocateContext
from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import GPTLocatorStrategy
from orchestrator.modules.slot_ocr.reader import OCRBox


def _context(raw_texts=None):
    return LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/shot.png",
        screen_width=1080, screen_height=2400, raw_texts=raw_texts or [], profile=None,
    )


def _answers(action="tap", target_text=None, target_region=None, confidence=0.8):
    return {
        "answers": {
            "action": {"choice": action, "confidence": confidence},
            "target": {"choice": {"text": target_text, "region": target_region}, "confidence": confidence},
        }
    }


class TestGPTLocatorStrategy:
    def test_tap_with_resolvable_text_target(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="SPIN", confidence=0.85))
        raw_texts = [("SPIN", (500, 2000), 0.9)]
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("ok_button", _context(raw_texts))
        assert result.success
        assert (result.data.x, result.data.y) == (500, 2000)
        assert result.data.confidence == 0.85

    def test_text_target_is_tapped_at_the_center_of_its_box_not_the_corner(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="OK"))
        context = _context([("OK", (500, 2000), 0.9)])
        context.text_boxes = [OCRBox(text="OK", bbox=(500, 2000, 600, 2040), confidence=0.9)]

        result = GPTLocatorStrategy(gpt_client).locate("ok_button", context)

        assert (result.data.x, result.data.y) == (550, 2020)

    def test_stop_action_fails(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(action="stop"))
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("ok_button", _context())
        assert not result.success
        assert result.error_code == "GPT_LOCATOR_NO_TAP"

    def test_underlying_gpt_failure_propagates(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.fail("API down", "GPT_API_ERROR")
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("ok_button", _context())
        assert not result.success
        assert result.error_code == "GPT_API_ERROR"

    def test_unresolved_target_fails(self):
        gpt_client = MagicMock()
        gpt_client.ask_with_image.return_value = Result.ok(_answers(target_text="GHOST"))
        strategy = GPTLocatorStrategy(gpt_client)

        result = strategy.locate("ok_button", _context(raw_texts=[]))
        assert not result.success
        assert result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"

    def test_gpt_client_is_injected_never_instantiated(self):
        gpt_client = MagicMock()
        strategy = GPTLocatorStrategy(gpt_client)
        assert strategy.gpt_client is gpt_client
        gpt_client.ask_with_image.assert_not_called()


@pytest.fixture
def real_context(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (1080, 2400), (10, 20, 30)).save(path)
    return LocateContext(
        device_id="dev", slot_name="bigbass", screenshot_path=str(path),
        screen_width=1080, screen_height=2400, raw_texts=[], profile=None,
    )


def _cell_answer(label, action="tap"):
    """Risposta del canale dedicato alla griglia (gpt_client.client)."""
    cell = label if action == "tap" else None
    return json.dumps({"cell": cell, "confidence": 0.8})


def _client(first, *cells):
    """gpt_client finto: il primo passo passa da ask_with_image, i passi
    A/B della griglia dal client OpenAI diretto (un JSON per chiamata)."""
    gpt_client = MagicMock()
    gpt_client.ask_with_image.return_value = first
    responses = []
    for cell in cells:
        if isinstance(cell, Exception):
            responses.append(cell)
        else:
            responses.append(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=cell))]))
    gpt_client.client.chat.completions.create.side_effect = responses
    return gpt_client


def _grid_calls(gpt_client):
    return gpt_client.client.chat.completions.create.call_args_list


def _grid_user_text(call):
    return call.kwargs["messages"][1]["content"][0]["text"]


class TestIconRefinement:
    def test_icon_is_located_in_three_grid_steps_never_at_the_3x3_cell_center(self, real_context):
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"),
        )

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.success
        assert len(_grid_calls(gpt_client)) == 3
        gpt_client.ask_with_image.assert_not_called()   # un'icona va diretta alla visione a griglia
        assert (result.data.x, result.data.y) != (180, 1200)   # centro 3x3 "middle-left"
        # A: B6 -> centro (405,1650); riquadro B (135,1350)-(675,1950); B: "C4" (6x6) -> (360,1700);
        # riquadro C (270,1600)-(450,1800) con griglia 4x4 (celle 45x50): "B2" -> (338,1675)
        assert (result.data.x, result.data.y) == (338, 1675)

    def test_step_c_failure_keeps_the_fine_cell_center(self, real_context):
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("ZZ9"),
        )

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.success
        assert (result.data.x, result.data.y) == (360, 1700)   # centro della cella B, non della 3x3

    def test_step_c_point_near_a_rejected_one_keeps_the_fine_cell_center(self, real_context):
        real_context.rejected_positions = [(293, 1665)]   # a 46 px dal punto C, a 75 px dal centro della cella B
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"),
        )

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert (result.data.x, result.data.y) == (360, 1700)

    def test_grid_steps_use_a_dedicated_prompt_not_the_generic_vision_prompt(self, real_context):
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("A1"), _cell_answer("A1"), _cell_answer("A1"),
        )
        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        call = _grid_calls(gpt_client)[0]
        assert "labeled grid" in call.kwargs["messages"][0]["content"]
        assert call.kwargs["response_format"] == {"type": "json_object"}
        assert "minus" in _grid_user_text(call).lower()
        assert "A1" in _grid_user_text(call) and "D8" in _grid_user_text(call)

    def test_non_icon_element_with_a_resolvable_text_needs_no_grid_steps(self, real_context):
        real_context.raw_texts = [("OK", (500, 2000), 0.9)]
        gpt_client = _client(Result.ok(_answers(target_text="OK")))

        result = GPTLocatorStrategy(gpt_client).locate("ok_button", real_context)

        assert result.success and (result.data.x, result.data.y) == (500, 2000)
        gpt_client.client.chat.completions.create.assert_not_called()

    def test_icon_elements_ignore_ocr_texts_entirely(self, real_context):
        """Nel log reale GPT aveva scelto il testo 'FAI LE TUE PUNTATE!' come 'spin'."""
        real_context.raw_texts = [("FAI LE TUE PUNTATE!", (287, 1419), 0.9)]
        gpt_client = _client(
            Result.ok(_answers(target_text="FAI LE TUE PUNTATE!")),
            _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"),
        )

        result = GPTLocatorStrategy(gpt_client).locate("spin", real_context)

        assert (result.data.x, result.data.y) == (338, 1675)
        gpt_client.ask_with_image.assert_not_called()

    @pytest.mark.parametrize("coarse", [
        _cell_answer("ZZ9"), _cell_answer("A1", action="stop"), "not json", json.dumps({"cell": 7}),
        json.dumps(["A1"]),
    ])
    def test_invalid_first_grid_step_fails_without_falling_back_to_the_coarse_cell(self, real_context, coarse):
        gpt_client = _client(Result.ok(_answers(target_region="middle-left")), coarse)

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert not result.success
        assert result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"

    def test_openai_error_is_logged_and_fails(self, real_context, caplog):
        import openai
        error = openai.APIConnectionError(request=MagicMock())
        gpt_client = _client(Result.ok(_answers(target_region="middle-left")), error)

        with caplog.at_level("WARNING"):
            result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert not result.success
        assert any("errore OpenAI" in record.getMessage() for record in caplog.records)

    def test_invalid_second_step_fails(self, real_context):
        gpt_client = _client(Result.ok(_answers(target_region="middle-left")), _cell_answer("B2"), _cell_answer("Z9"))
        assert not GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context).success

    def test_gpt_answers_are_logged_for_debugging(self, real_context, caplog):
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"),
        )
        with caplog.at_level("INFO"):
            GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        messages = [record.getMessage() for record in caplog.records]
        assert any("GPT risponde cella='B6'" in m for m in messages)
        assert any("GPT risponde cella='C4'" in m for m in messages)
        assert any("GPT risponde cella='B2'" in m for m in messages)

    def test_temporary_grid_images_are_removed(self, real_context, monkeypatch):
        seen = set()   # PIL (salvataggio) e la lettura base64 aprono lo stesso file
        original_open = open

        def spy_open(path, *args, **kwargs):
            if "autopilot_grid_" in str(path):
                seen.add(str(path))
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", spy_open)
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("A1"), _cell_answer("A1"), _cell_answer("A1"),
        )
        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert len(seen) == 3
        assert not any(os.path.exists(path) for path in seen)


class TestRejectedPointsAndDescriptions:
    def _first_grid_text(self, element, real_context):
        gpt_client = _client(None, _cell_answer("ZZ9"))
        GPTLocatorStrategy(gpt_client).locate(element, real_context)
        return _grid_user_text(_grid_calls(gpt_client)[0])

    def test_bet_minus_description_excludes_the_info_icon(self, real_context):
        text = self._first_grid_text("bet_minus", real_context)
        assert "NEVER the spin button itself, the 'i' (info) icon" in text
        assert "left-pointing arrow" in text          # anche le frecce "<" contano come "-"

    def test_close_button_has_a_description(self, real_context):
        assert "close" in self._first_grid_text("close_button", real_context).lower()

    def test_rejected_points_are_mentioned_in_the_question(self, real_context):
        real_context.rejected_positions = [(46, 2048)]
        text = self._first_grid_text("bet_minus", real_context)
        assert "WRONG element" in text and "4% from left" in text

    def test_rejected_points_are_also_passed_to_the_grid_steps(self, real_context):
        real_context.rejected_positions = [(46, 2048)]
        gpt_client = _client(
            Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"),
        )
        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert "WRONG element" in _grid_user_text(_grid_calls(gpt_client)[0])

    def test_fine_cells_near_a_rejected_point_are_not_offered(self, real_context):
        # Punto scartato al centro della cella A1 fine: quella cella non e' un candidato.
        real_context.rejected_positions = [(135, 1350 + 50)]
        gpt_client = _client(Result.ok(_answers(target_region="middle-left")), _cell_answer("B6"), _cell_answer("A1"))
        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        fine_text = _grid_user_text(_grid_calls(gpt_client)[1])
        labels_line = fine_text.split("Valid cell labels: ")[1].split(".\n")[0].split(", ")
        assert "A1" not in labels_line and "F6" in labels_line
        assert not result.success   # GPT ha scelto proprio la cella tolta


class TestLocateDescribed:
    def test_operator_phrase_becomes_the_element_description_in_every_step(self, real_context):
        gpt_client = _client(Result.ok(_answers(action="stop")), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        point = GPTLocatorStrategy(gpt_client).locate_described(
            "l'icona delle monete impilate in basso al centro", real_context,
        )

        assert point == (338, 1675)
        gpt_client.ask_with_image.assert_not_called()    # nessun passo 0: e' gia' un'icona
        for call in _grid_calls(gpt_client):
            assert "l'icona delle monete impilate in basso al centro" in _grid_user_text(call)

    def test_returns_none_when_gpt_cannot_find_it(self, real_context):
        gpt_client = _client(Result.ok(_answers(action="stop")), _cell_answer("A1", action="stop"))

        assert GPTLocatorStrategy(gpt_client).locate_described("boh", real_context) is None


class TestCurrentBetHint:
    def _grid_texts(self, element, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))
        GPTLocatorStrategy(gpt_client).locate(element, real_context)
        return [_grid_user_text(call) for call in _grid_calls(gpt_client)]

    def test_bet_minus_names_the_current_total_bet_for_multi_row_panels_in_every_step(self, real_context):
        real_context.slot_ocr_state = {"posta": 2.0}

        texts = self._grid_texts("bet_minus", real_context)

        assert len(texts) == 3 and all("2.00" in t and "TOTAL bet" in t for t in texts)

    def test_no_hint_for_other_elements_or_without_a_bet_reading(self, real_context):
        real_context.slot_ocr_state = {"posta": 2.0}
        assert all("TOTAL bet" not in t for t in self._grid_texts("spin", real_context))

        real_context.slot_ocr_state = {}
        assert all("TOTAL bet" not in t for t in self._grid_texts("bet_minus", real_context))


class TestNotVisibleIsNotAGuess:
    """Nel log reale GPT aveva detto "null" al passo C e il codice aveva tappato lo stesso: era lo spin."""

    def test_an_explicit_null_at_the_zoomed_step_means_no_tap_at_all(self, real_context, caplog):
        gpt_client = _client(
            None, _cell_answer("B6"), _cell_answer("C4"), json.dumps({"cell": None, "confidence": 0.9}),
        )

        with caplog.at_level("WARNING"):
            result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert not result.success and result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"
        assert any("non vede l'elemento" in r.getMessage() for r in caplog.records)

    def test_an_unreadable_answer_at_the_zoomed_step_still_keeps_the_fine_cell(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), "not json")

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.success and (result.data.x, result.data.y) == (360, 1700)

    def test_an_invalid_label_at_the_zoomed_step_still_keeps_the_fine_cell(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("ZZ9"))

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.success and (result.data.x, result.data.y) == (360, 1700)


class TestAvoidZones:
    def test_the_zone_is_named_in_every_grid_question(self, real_context):
        real_context.avoid_zones = [(540, 1775, 90, "SPIN button")]
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        for call in _grid_calls(gpt_client):
            text = _grid_user_text(call)
            assert "SPIN button" in text and "50% from left" in text and "74% from top" in text

    def test_fine_cells_inside_the_zone_are_not_offered(self, real_context):
        real_context.avoid_zones = [(360, 1700, 120, "SPIN button")]
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        text = _grid_user_text(_grid_calls(gpt_client)[1])
        labels = text.split("Valid cell labels: ")[1].split(".\n")[0].split(", ")
        assert "C4" not in labels and "F6" in labels
        assert not result.success                         # GPT ha scelto proprio una cella esclusa

    def test_a_step_c_point_inside_the_zone_keeps_the_fine_cell_instead(self, real_context):
        real_context.avoid_zones = [(338, 1675, 20, "SPIN button")]     # solo il punto del passo C
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert (result.data.x, result.data.y) == (360, 1700)


def test_grid_prompt_asks_gpt_to_read_the_amounts_first():
    from orchestrator.modules.slot_autopilot.strategies import gpt_locator_strategy

    assert "values_seen" in gpt_locator_strategy._GRID_SYSTEM_PROMPT
    assert "lowest" in gpt_locator_strategy._GRID_SYSTEM_PROMPT


def _kind_answer(kind):
    return json.dumps({"kind": kind, "confidence": 0.9})


def _rate_limit():
    import httpx
    import openai
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return openai.RateLimitError("Please try again in 100ms.", response=response, body=None)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("orchestrator.core.openai_retry.time.sleep", lambda seconds: None)


class TestIconIsVerifiedBeforeTheFirstTap:
    def test_a_point_that_is_the_expected_kind_is_proposed(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"), _kind_answer("minus_button"))

        result = GPTLocatorStrategy(gpt_client, verify_icons=True).locate("bet_minus", real_context)

        assert result.success

    def test_a_point_that_is_something_else_is_not_proposed(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"), _kind_answer("reels_or_symbols"))

        result = GPTLocatorStrategy(gpt_client, verify_icons=True).locate("bet_minus", real_context)

        assert not result.success and result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"

    def test_the_menu_icon_is_not_accepted_for_the_bet_menu_toggle(self, real_context):
        gpt_client = _client(None, _cell_answer("A1"), _cell_answer("A1"), _cell_answer("A1"), _kind_answer("menu_icon"))

        result = GPTLocatorStrategy(gpt_client, verify_icons=True).locate("bet_menu_toggle", real_context)

        assert not result.success

    def test_the_crop_sent_for_verification_is_small(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"), _kind_answer("minus_button"))

        GPTLocatorStrategy(gpt_client, verify_icons=True).locate("bet_minus", real_context)

        last = _grid_calls(gpt_client)[-1]
        assert "center of this crop" in last.kwargs["messages"][1]["content"][0]["text"]

    def test_operator_phrases_are_not_second_guessed(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        point = GPTLocatorStrategy(gpt_client, verify_icons=True).locate_described("le monete", real_context)

        assert point is not None and len(_grid_calls(gpt_client)) == 3

    def test_verification_is_off_unless_asked_for(self, real_context):
        gpt_client = _client(None, _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        assert GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context).success
        assert len(_grid_calls(gpt_client)) == 3


class TestRateLimit:
    def test_a_temporary_limit_is_waited_out_and_the_grid_goes_on(self, real_context, no_sleep):
        gpt_client = _client(None, _rate_limit(), _cell_answer("B6"), _cell_answer("C4"), _cell_answer("B2"))

        assert GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context).success

    def test_a_persistent_limit_is_unavailable_not_not_visible(self, real_context, no_sleep):
        gpt_client = _client(None, *[_rate_limit() for _ in range(5)])

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert not result.success and result.error_code == "GPT_LOCATOR_UNAVAILABLE"

    def test_a_clear_not_visible_answer_is_still_unresolved(self, real_context):
        gpt_client = _client(None, _cell_answer("B6", action="none"))

        result = GPTLocatorStrategy(gpt_client).locate("bet_minus", real_context)

        assert result.error_code == "GPT_LOCATOR_UNRESOLVED_TARGET"
