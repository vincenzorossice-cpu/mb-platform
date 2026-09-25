import os
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.result import Result
from orchestrator.tests.validation.test_slot_autonomous import (
    MAX_REPEAT_ATTEMPTS_INSIDE_MENU,
    MAX_REPEAT_ATTEMPTS_OUTSIDE_MENU,
    SLOT_MEMORY_DIR,
    _persist_slot_memory,
    _slot_memory_path,
    ask_human_help,
    count_consecutive_same_spot_failures,
    determine_phase,
    find_untried_elements,
    load_slot_memory,
    nearby_offsets,
    normalize_texts,
    region_to_point,
    resize_screenshot_for_vision,
    resolve_target,
    run_autonomous_session,
    save_slot_memory,
)


def _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=None, raw_texts=None):
    return {
        "saldo": saldo,
        "posta": posta,
        "vincita": None,
        "spin_button_coords": spin_button_coords,
        "is_active": False,
        "raw_texts": raw_texts or [],
    }


def _text(text, x, y, conf=0.9):
    return (text, (x, y), conf)


def _target(text=None, region=None):
    return {"text": text, "region": region} if (text or region) else None


def _answers(action, target=None, screen_state="idle", confidence=0.9):
    return {"answers": {
        "action": {"choice": action, "confidence": confidence},
        "target": {"choice": target, "confidence": confidence},
        "screen_state": {"choice": screen_state, "confidence": confidence},
    }}


class TestResizeScreenshotForVision:

    def test_halves_dimensions_and_returns_original_size(self, tmp_path):
        """Passa scale=0.5 esplicitamente: il test verifica la matematica della
        funzione, indipendentemente da quale sia il default VISION_RESIZE_SCALE
        attualmente configurato per la sessione reale."""
        from PIL import Image

        path = tmp_path / "shot.png"
        Image.new("RGB", (1080, 2400), color="white").save(path)

        resized_path, w, h, orig_w, orig_h = resize_screenshot_for_vision(str(path), scale=0.5)

        assert (w, h) == (540, 1200)
        assert (orig_w, orig_h) == (1080, 2400)
        assert os.path.exists(resized_path)
        assert Image.open(resized_path).size == (540, 1200)

        os.remove(resized_path)


class TestRegionToPoint:

    def test_known_regions_are_generic_geometry_not_slot_specific(self):
        assert region_to_point("top-left", 1080, 2400) == (180, 400)
        assert region_to_point("center", 1080, 2400) == (540, 1200)
        assert region_to_point("bottom-right", 1080, 2400) == (900, 2000)
        # Stessa griglia, dimensioni diverse: dimostra che non c'e' nulla hardcoded.
        assert region_to_point("bottom-right", 540, 1200) == (450, 1000)

    def test_unknown_region_returns_none(self):
        assert region_to_point("nowhere", 1080, 2400) is None

    def test_none_region_returns_none(self):
        assert region_to_point(None, 1080, 2400) is None


class TestResolveTarget:

    def test_exact_text_match_uses_ocr_measured_coordinates(self):
        raw_texts = [_text("SPIN", 543, 2107)]
        assert resolve_target(_target(text="SPIN"), raw_texts, 1080, 2400) == (543, 2107)

    def test_case_insensitive_match(self):
        raw_texts = [_text("SPIN", 543, 2107)]
        assert resolve_target(_target(text="spin"), raw_texts, 1080, 2400) == (543, 2107)

    def test_fuzzy_match(self):
        raw_texts = [_text("PUNTATA €10.00", 100, 2000)]
        assert resolve_target(_target(text="PUNTATA  10.00"), raw_texts, 1080, 2400) == (100, 2000)

    def test_falls_back_to_region_when_text_not_found(self):
        raw_texts = [_text("SPIN", 543, 2107)]
        result = resolve_target({"text": "NOPE", "region": "bottom-right"}, raw_texts, 1080, 2400)
        assert result == (900, 2000)

    def test_region_only_when_no_text_given(self):
        assert resolve_target(_target(region="center"), [], 1080, 2400) == (540, 1200)

    def test_no_target_returns_none(self):
        assert resolve_target(None, [], 1080, 2400) is None

    def test_no_text_and_no_valid_region_returns_none(self):
        assert resolve_target({"text": "NOPE", "region": "nowhere"}, [], 1080, 2400) is None


class TestDeterminePhase:

    def test_above_minimum_is_setup(self):
        assert determine_phase(10.0) == "setup"

    def test_at_minimum_is_playing(self):
        assert determine_phase(0.10) == "playing"

    def test_below_minimum_is_playing(self):
        assert determine_phase(0.05) == "playing"

    def test_unknown_bet_is_playing(self):
        assert determine_phase(None) == "playing"


class TestNormalizeTexts:

    def test_case_and_whitespace_insensitive(self):
        raw = [_text("  PUNTATA   €10.00 ", 10, 10), _text("Spin", 500, 900)]
        assert normalize_texts(raw) == {"puntata €10.00", "spin"}

    def test_empty_text_ignored(self):
        assert normalize_texts([_text("   ", 0, 0)]) == set()


class TestFindUntriedElements:

    def test_returns_texts_far_from_tapped_targets(self):
        raw_texts = [_text("SPIN", 540, 1200), _text("PUNTATA €10.00", 100, 2000)]
        tapped = [{"x": 540, "y": 1200, "screen_changed": False}]
        assert find_untried_elements(raw_texts, tapped) == ["PUNTATA €10.00"]

    def test_returns_all_when_nothing_tapped_yet(self):
        raw_texts = [_text("SPIN", 540, 1200), _text("PUNTATA €10.00", 100, 2000)]
        assert find_untried_elements(raw_texts, []) == ["SPIN", "PUNTATA €10.00"]

    def test_within_tolerance_counts_as_tried(self):
        raw_texts = [_text("SPIN", 545, 1205)]
        tapped = [{"x": 540, "y": 1200, "screen_changed": False}]
        assert find_untried_elements(raw_texts, tapped) == []


class TestNearbyOffsets:

    def test_generates_four_cardinal_points_per_distance(self):
        offsets = nearby_offsets(500, 500, offsets=(40,))
        assert set(offsets) == {(460, 500), (540, 500), (500, 460), (500, 540)}

    def test_multiple_distances_are_all_included(self):
        offsets = nearby_offsets(500, 500, offsets=(40, 80))
        assert len(offsets) == 8
        assert (420, 500) in offsets
        assert (500, 580) in offsets


class TestCountConsecutiveSameSpotFailures:

    def test_counts_matching_recent_failures(self):
        tapped = [
            {"x": 100, "y": 100, "screen_changed": True},
            {"x": 500, "y": 500, "screen_changed": False},
            {"x": 500, "y": 500, "screen_changed": False},
        ]
        assert count_consecutive_same_spot_failures(tapped, 500, 500) == 2

    def test_stops_at_first_non_matching_entry_going_backwards(self):
        tapped = [
            {"x": 500, "y": 500, "screen_changed": False},
            {"x": 500, "y": 500, "screen_changed": True},  # questo tap e' riuscito: interrompe la serie
            {"x": 500, "y": 500, "screen_changed": False},
        ]
        assert count_consecutive_same_spot_failures(tapped, 500, 500) == 1

    def test_no_matches_returns_zero(self):
        tapped = [{"x": 100, "y": 100, "screen_changed": False}]
        assert count_consecutive_same_spot_failures(tapped, 500, 500) == 0


class TestAskHumanHelp:
    """Non deve mai aprire uno screenshot: solo GPT-4o-mini lo vede (via Vision
    API), l'operatore interagisce esclusivamente da terminale."""

    def test_stop_returns_stop_tuple(self):
        with patch("builtins.input", return_value="stop"):
            assert ask_human_help(10.0, 0.10) == ("stop",)

    def test_skip_returns_skip_tuple(self):
        with patch("builtins.input", return_value="skip"):
            assert ask_human_help(10.0, 0.10) == ("skip",)

    def test_coordinates_are_parsed(self):
        with patch("builtins.input", return_value="540, 600"):
            assert ask_human_help(10.0, 0.10) == ("coords", 540, 600)

    def test_free_text_is_returned_as_hint(self):
        with patch("builtins.input", return_value="tap the coins icon"):
            assert ask_human_help(10.0, 0.10) == ("text", "tap the coins icon")

    def test_malformed_coordinates_fall_back_to_text(self):
        with patch("builtins.input", return_value="540, abc"):
            assert ask_human_help(10.0, 0.10) == ("text", "540, abc")

    def test_unknown_bet_does_not_crash(self):
        with patch("builtins.input", return_value="skip") as mock_input:
            assert ask_human_help(None, 0.10) == ("skip",)
        mock_input.assert_called_once()


class TestSlotMemory:

    def test_load_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_slot_memory("Nonexistent Slot") == {"known_spin_coords": None, "learned_hints": []}

    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        save_slot_memory("Pirots 5", {"known_spin_coords": (540, 2100), "learned_hints": ["tap coin icon"]})
        assert load_slot_memory("Pirots 5") == {"known_spin_coords": (540, 2100), "learned_hints": ["tap coin icon"]}

    def test_persist_merges_without_losing_existing_data(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        save_slot_memory("Pirots 5", {"known_spin_coords": (540, 2100), "learned_hints": ["hint A"]})
        _persist_slot_memory("Pirots 5", None, ["hint B"])
        memory = load_slot_memory("Pirots 5")
        assert memory["known_spin_coords"] == (540, 2100)
        assert set(memory["learned_hints"]) == {"hint A", "hint B"}

    def test_persist_does_nothing_without_slot_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _persist_slot_memory(None, (1, 2), ["x"])
        assert not os.path.exists(SLOT_MEMORY_DIR)

    def test_corrupted_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        os.makedirs(SLOT_MEMORY_DIR, exist_ok=True)
        with open(_slot_memory_path("Broken"), "w") as f:
            f.write("not json")
        assert load_slot_memory("Broken") == {"known_spin_coords": None, "learned_hints": []}


@pytest.fixture
def mocks(tmp_path):
    from PIL import Image

    screenshot_path = str(tmp_path / "shot.png")
    Image.new("RGB", (1080, 2400), color="white").save(screenshot_path)

    with patch("orchestrator.tests.validation.test_slot_autonomous.GPTDecisionClient") as mock_gpt_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.ScreenReader") as mock_screen_reader_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.TouchInjector") as mock_touch_injector_cls, \
         patch("orchestrator.tests.validation.test_slot_autonomous.SlotOCRReader") as mock_ocr_reader_cls, \
         patch("builtins.input", return_value=""), \
         patch("orchestrator.tests.validation.test_slot_autonomous.time.sleep"):
        screen_reader = MagicMock()
        screen_reader._screenshot.return_value = Result.ok(screenshot_path)
        mock_screen_reader_cls.return_value = screen_reader

        touch_injector = MagicMock()
        touch_injector.tap_coordinates.return_value = Result.ok(True)
        mock_touch_injector_cls.return_value = touch_injector

        ocr_reader = MagicMock()
        mock_ocr_reader_cls.return_value = ocr_reader

        engine = MagicMock()
        mock_gpt_cls.return_value = engine

        yield {
            "screen_reader": screen_reader, "touch_injector": touch_injector,
            "ocr_reader": ocr_reader, "engine": engine, "screenshot_path": screenshot_path,
        }


class TestRunAutonomousSession:

    def test_stops_immediately_when_engine_says_stop(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(270, 1900))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_auto_stop_on_insufficient_balance_never_calls_engine(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=0.05)

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["engine"].ask_with_image.assert_not_called()
        mocks["touch_injector"].tap_coordinates.assert_not_called()

    def test_tap_uses_ocr_measured_coordinates_for_text_target(self, mocks):
        """GPT non restituisce mai pixel: sceglie un testo tra i candidati OCR
        e il sistema tappa esattamente dove l'OCR ha misurato quell'elemento
        (nessun 'grounding spaziale' richiesto a GPT, vedi slot_anatomy.md)."""
        raw_texts = [_text("SPIN", 543, 2107)]
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=0.10, spin_button_coords=(543, 2107), raw_texts=raw_texts,
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(text="SPIN"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_called_once_with("dev", 543, 2107)

    def test_tap_falls_back_to_region_when_no_text_candidate_matches(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, posta=0.10, raw_texts=[])
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="bottom-right"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_called_once_with("dev", 900, 2000)

    def test_ask_with_image_receives_resized_dimensions(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0)
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        call_args = mocks["engine"].ask_with_image.call_args
        # (screenshot_path, state_text, questions, image_width, image_height)
        assert call_args.args[3] == 1080
        assert call_args.args[4] == 2400
        assert call_args.args[0].endswith("_small.png")

    def test_total_played_is_sum_of_bets_not_balance_diff(self, mocks, capsys):
        """Ogni tap reale forza un refresh OCR al passo successivo: il saldo
        cala 100 -> 99.95 (vincita parziale, cala MENO della posta) -> 99.85
        (spin normale) -> 105.85 (vincita netta, il saldo SALE). total_played
        deve usare sempre la posta OCR (0.10) per i primi due spin (non 0.05,
        il calo reale) e NON decrementare/azzerarsi sul terzo spin vincente
        (il saldo non e' sceso, quindi quello spin non viene proprio contato):
        mai un calcolo basato su saldo_iniziale - saldo_attuale."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=99.95, posta=0.10, spin_button_coords=(540, 1200)),  # dopo step1: vincita parziale (-0.05)
            _ocr_state(saldo=99.85, posta=0.10, spin_button_coords=(540, 1200)),  # dopo step2: spin normale (-0.10)
            _ocr_state(saldo=105.85, posta=0.10, spin_button_coords=(540, 1200)),  # dopo step3: vincita netta (saldo sale)
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 0.20 EUR" in out
        assert "Net profit/loss: +5.85 EUR" in out

    def test_balance_drop_not_counted_when_screen_state_not_trusted(self, mocks, capsys):
        """slot_anatomy.md sez. 7: i numeri sono attendibili solo in idle (o
        modal). Se l'ultimo screen_state noto era 'spinning', un calo di saldo
        NON deve essere sommato a total_played (potrebbe essere un valore a
        meta' animazione)."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=90.0, posta=0.10, spin_button_coords=(540, 1200)),  # calo enorme, ma stato non fidato
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"), screen_state="spinning")),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 0.00 EUR" in out
        assert "non conteggiato per prudenza" in out

    def test_setup_phase_taps_do_not_count_toward_total_played(self, mocks, capsys):
        """Con la posta sopra il minimo (fase 'setup'), un tap serve ad
        aggiustare il menu posta, non a giocare: non deve incrementare
        total_played."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left"))),  # tap sul menu posta, non sullo spin
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Totale giocato: 0.00 EUR" in out

    def test_learns_spin_coords_from_observed_spin_and_blocks_next_attempt(self, mocks):
        """Bug reale: quando l'OCR non etichetta mai il tasto SPIN, il primo
        tap che causa uno spin non e' bloccabile in anticipo (non sappiamo
        ancora dove sia lo SPIN) - ma una volta osservato il calo di saldo, il
        sistema impara quelle coordinate e blocca i tentativi successivi
        durante il setup, invece di limitarsi a rilevarli dopo il fatto."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=5000.0, posta=10.0, spin_button_coords=None),  # OCR non trova mai lo SPIN
            _ocr_state(saldo=4990.0, posta=10.0, spin_button_coords=None),  # dopo il 1o tap: spin avvenuto
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),  # 1o tentativo: non bloccabile, spin ancora sconosciuto
            Result.ok(_answers("tap", _target(region="center"))),  # 2o tentativo identico: ORA deve essere bloccato
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert mocks["touch_injector"].tap_coordinates.call_count == 1  # solo il primo tap eseguito
        third_call_state_text = mocks["engine"].ask_with_image.call_args_list[2].args[1]
        assert "BLOCKED: You tried to tap the SPIN button" in third_call_state_text

    def test_spin_tap_blocked_during_setup_phase(self, mocks):
        """Bug reale: GPT tappa lo SPIN mentre la posta e' ancora a 10 EUR
        (fase setup). Il tap NON deve essere eseguito, e la chiamata
        successiva a ask_with_image deve ricevere una nota correttiva."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),  # tenta di spinnare con posta alta -> bloccato
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()
        assert mocks["engine"].ask_with_image.call_count == 2

        second_call_state_text = mocks["engine"].ask_with_image.call_args_list[1].args[1]
        assert "BLOCKED: You tried to tap the SPIN button but the bet is still 10.00" in second_call_state_text
        assert "DO NOT tap the SPIN button" in second_call_state_text

    def test_hitl_stop_ends_session(self, mocks):
        """Se GPT continua a non abbassare la posta per MAX_CONSECUTIVE_SETUP_STEPS
        step, il sistema chiede aiuto all'operatore via terminale; se l'operatore
        scrive 'stop', la sessione finisce immediatamente."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("tap", _target(region="top-left")))

        with patch(
            "orchestrator.tests.validation.test_slot_autonomous.ask_human_help", return_value=("stop",),
        ) as mock_ask_human:
            ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is False
        assert mock_ask_human.call_count == 1
        assert mocks["engine"].ask_with_image.call_count == 5

    def test_hitl_skip_lets_gpt_keep_trying(self, mocks):
        """Se l'operatore scrive 'skip', il sistema resetta il contatore e
        lascia che GPT-4o riprovi da solo senza fermarsi."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left")))
        ] * 5 + [Result.ok(_answers("stop"))]

        with patch(
            "orchestrator.tests.validation.test_slot_autonomous.ask_human_help", return_value=("skip",),
        ) as mock_ask_human:
            ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is True
        assert mock_ask_human.call_count == 1
        assert mocks["engine"].ask_with_image.call_count == 6

    def test_hitl_text_hint_accumulates_and_persists_across_turns(self, mocks):
        """Il suggerimento testuale dell'operatore deve restare visibile a GPT
        per piu' turni consecutivi, non sparire dopo il primo (Causa C)."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200),
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left")))
        ] * 5 + [
            Result.ok(_answers("tap", _target(region="top-right"))),
            Result.ok(_answers("stop")),
        ]

        with patch(
            "orchestrator.tests.validation.test_slot_autonomous.ask_human_help",
            return_value=("text", "tap the coins icon top left"),
        ):
            ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is True
        sixth_call = mocks["engine"].ask_with_image.call_args_list[5].args[1]
        seventh_call = mocks["engine"].ask_with_image.call_args_list[6].args[1]
        assert "The human operator suggests: tap the coins icon top left" in sixth_call
        assert "The human operator suggests: tap the coins icon top left" in seventh_call

    def test_hitl_coords_resolve_session_when_bet_actually_lowers(self, mocks, capsys):
        """Se l'operatore da' coordinate e quel tap abbassa davvero la posta
        (verificato rileggendo l'OCR), il sistema riprende normalmente."""
        state = {"posta": 10.0}

        def ocr_side_effect(*_args, **_kwargs):
            return _ocr_state(saldo=100.0, posta=state["posta"], spin_button_coords=(540, 1200))

        mocks["ocr_reader"].read_slot_state.side_effect = ocr_side_effect

        def tap_side_effect(_device_id, x, y):
            if (x, y) == (700, 600):
                state["posta"] = 0.10
            return Result.ok(True)

        mocks["touch_injector"].tap_coordinates.side_effect = tap_side_effect

        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left")))
        ] * 5 + [Result.ok(_answers("stop"))]

        with patch(
            "orchestrator.tests.validation.test_slot_autonomous.ask_human_help", return_value=("coords", 700, 600),
        ):
            ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is True
        assert state["posta"] == 0.10
        out = capsys.readouterr().out
        assert "Posta abbassata con successo" in out

    def test_screen_state_modal_gives_menu_patience_even_with_noisy_static_ocr(self, mocks):
        """Bug reale: in produzione l'OCR legge testi leggermente diversi ad
        ogni lettura anche a schermo fermo, quindi il vecchio confronto testi
        non rilevava mai il menu aperto e un tap ripetuto a vuoto non veniva
        mai bloccato per pazientare (l'anti-loop restava sempre alla soglia
        'fuori menu'). Ora screen_state='modal' (dichiarato da GPT) basta da
        solo, anche se i testi OCR letti sono SEMPRE identici (0 new/disappeared)."""
        same_raw_texts = [_text("PUNTATA €10.00", 100, 2000)]  # mai cambia: simula la staticita'/rumore OCR
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=None, raw_texts=same_raw_texts,
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="bottom-left"), screen_state="idle")),  # apre il menu
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 1 nel menu
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 2
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 3
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 4 (soglia menu)
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is True
        # Se menu_open fosse rimasto False (vecchia logica basata sul diff OCR,
        # che qui non cambia mai), la soglia sarebbe stata 2 e i tentativi 3-4
        # sarebbero stati bloccati: qui invece vengono tutti eseguiti.
        assert mocks["touch_injector"].tap_coordinates.call_count == 1 + MAX_REPEAT_ATTEMPTS_INSIDE_MENU

    def test_repeat_tap_without_effect_is_blocked_with_untried_suggestions(self, mocks):
        """Se GPT tappa lo stesso punto morto due volte senza alcun effetto
        (ne' sul saldo ne' sui testi OCR, e screen_state resta 'idle'), il
        terzo tentativo identico deve essere bloccato e a GPT vanno suggeriti
        elementi mai provati."""
        raw_texts = [_text("SPIN", 540, 1200), _text("PUNTATA €0.10", 100, 2000)]
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=raw_texts,
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left"))),  # punto morto, primo tentativo
            Result.ok(_answers("tap", _target(region="top-left"))),  # stesso punto, ancora nulla
            Result.ok(_answers("tap", _target(region="top-left"))),  # terzo tentativo identico -> bloccato
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert mocks["touch_injector"].tap_coordinates.call_count == 2  # solo i primi 2 tap eseguiti
        fourth_call_state_text = mocks["engine"].ask_with_image.call_args_list[3].args[1]
        assert "You tapped this area twice with no effect" in fourth_call_state_text
        assert "coins icon, settings gear, +/- buttons" in fourth_call_state_text

    def test_repeat_tap_inside_menu_gets_more_patience_and_nearby_suggestions(self, mocks):
        """Dentro un menu (screen_state='modal'), 2 tentativi identici NON
        devono bastare per bloccare (soglia piu' alta), e quando scatta il
        blocco il messaggio deve suggerire coordinate VICINE da affinare, non
        di abbandonare l'area."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=[],
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="bottom-left"), screen_state="idle")),  # apre il menu
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 1
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 2 -> NON bloccato
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 3
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 4 (soglia menu)
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # tentativo 5 -> ORA bloccato
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20)

        assert ok is True
        assert mocks["touch_injector"].tap_coordinates.call_count == 1 + MAX_REPEAT_ATTEMPTS_INSIDE_MENU
        last_call_state_text = mocks["engine"].ask_with_image.call_args_list[-1].args[1]
        assert "Try one of these nearby coordinates instead" in last_call_state_text
        assert "COMPLETELY DIFFERENT" not in last_call_state_text

    def test_repeat_tap_outside_menu_still_blocks_after_two_like_before(self, mocks):
        """Fuori da un menu (screen_state='idle') il comportamento precedente
        (soglia 2) non deve regredire: e' li' un segnale affidabile di errore
        vero."""
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=[],
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left"))),
            Result.ok(_answers("tap", _target(region="top-left"))),
            Result.ok(_answers("tap", _target(region="top-left"))),  # 3° tentativo -> bloccato, come prima del fix
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert mocks["touch_injector"].tap_coordinates.call_count == MAX_REPEAT_ATTEMPTS_OUTSIDE_MENU

    def test_menu_closed_transition_is_logged(self, mocks, capsys):
        """Quando lo stato passa da 'modal' a un altro stato, il sistema deve
        segnalarlo esplicitamente: e' l'unico momento in cui la posta reale
        diventa visibile via OCR per un menu di questo tipo."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=[]),
            _ocr_state(saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=[]),
            _ocr_state(saldo=100.0, posta=0.20, spin_button_coords=(540, 1200), raw_texts=[]),
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="bottom-left"), screen_state="idle")),  # apre il menu
            Result.ok(_answers("tap", _target(region="top-left"), screen_state="modal")),  # seleziona 0.20
            Result.ok(_answers("stop", screen_state="idle")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        out = capsys.readouterr().out
        assert "Menu chiuso: posta ora 0.20 EUR" in out

    def test_stuck_nudge_after_threshold_steps_without_change(self, mocks):
        """Anche tappando punti DIVERSI ogni volta, se lo schermo non cambia
        mai per STUCK_SCREEN_THRESHOLD step, deve comparire un avviso generico
        a provare qualcosa di completamente diverso."""
        raw_texts = [_text("SPIN", 540, 1200)]
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(
            saldo=100.0, posta=10.0, spin_button_coords=(540, 1200), raw_texts=raw_texts,
        )
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left"))),
            Result.ok(_answers("tap", _target(region="top-right"))),
            Result.ok(_answers("tap", _target(region="bottom-left"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        fourth_call_state_text = mocks["engine"].ask_with_image.call_args_list[3].args[1]
        assert "has not changed for" in fourth_call_state_text

    def test_stops_automatically_once_target_reached_without_asking_again(self, mocks):
        """Il controllo su total_played >= target deve avvenire PRIMA di
        interpellare l'engine: appena il target e' raggiunto, l'engine non va
        piu' chiamato (niente overshoot come nel bug reale 35 invece di 30)."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=0.10, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=99.90, posta=0.10, spin_button_coords=(540, 1200)),  # dopo step1 tap -> total_played 0.10
            _ocr_state(saldo=99.80, posta=0.10, spin_button_coords=(540, 1200)),  # dopo step2 tap -> total_played 0.20 == target
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("tap", _target(region="center"))),
        ]

        ok = run_autonomous_session("dev", target_amount=0.20, max_steps=10)

        assert ok is True
        assert mocks["engine"].ask_with_image.call_count == 2
        assert mocks["ocr_reader"].read_slot_state.call_count == 3

    def test_missing_target_forces_ocr_refresh_without_tapping(self, mocks):
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", None)),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()
        assert mocks["ocr_reader"].read_slot_state.call_count == 2

    def test_non_spin_tap_forces_ocr_refresh_next_step(self, mocks):
        """Se il tap non e' vicino al tasto spin noto, l'OCR va riletto al
        prossimo step invece di riusare la cadenza ogni 3 step."""
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),  # initial
            _ocr_state(saldo=100.0, spin_button_coords=(540, 1200)),  # forced refresh (menu posta lontano dallo spin)
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="top-left"))),  # lontano dallo spin (540,1200)
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert mocks["ocr_reader"].read_slot_state.call_count == 2

    def test_engine_failure_returns_false(self, mocks):
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0)
        mocks["engine"].ask_with_image.return_value = Result.fail("boom", "GPT_DECISION_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is False

    def test_screenshot_failure_returns_false(self, mocks):
        mocks["screen_reader"]._screenshot.return_value = Result.fail("adb error", "ADB_TIMEOUT")

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is False
        mocks["engine"].ask_with_image.assert_not_called()

    def test_save_screenshots_writes_files_when_enabled(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(540, 1200))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, save_screenshots=True)

        assert ok is True
        saved = tmp_path / "screenshots" / "step_0001.png"
        assert saved.exists()

    def test_no_screenshots_saved_by_default(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, spin_button_coords=(540, 1200))
        mocks["engine"].ask_with_image.return_value = Result.ok(_answers("stop"))

        run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert not (tmp_path / "screenshots").exists()

    def test_slot_name_seeds_known_spin_coords_and_hints_from_memory(self, mocks, tmp_path, monkeypatch):
        """La memoria di una slot gia' giocata in precedenza da' subito al
        sistema un vantaggio: le coordinate spin imparate bloccano un tentativo
        di spin prematuro fin dal primo step di questa nuova sessione, e i
        suggerimenti confermati utili sono gia' attivi."""
        monkeypatch.chdir(tmp_path)
        save_slot_memory("Pirots 5", {"known_spin_coords": (540, 1200), "learned_hints": ["tap the bet display"]})

        mocks["ocr_reader"].read_slot_state.return_value = _ocr_state(saldo=100.0, posta=10.0, spin_button_coords=None)
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),  # (540,1200) = spin noto dalla memoria
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, slot_name="Pirots 5")

        assert ok is True
        mocks["touch_injector"].tap_coordinates.assert_not_called()  # bloccato subito, senza doverlo "riosservare"
        second_call_state_text = mocks["engine"].ask_with_image.call_args_list[1].args[1]
        assert "The human operator suggests: tap the bet display" in second_call_state_text

    def test_learned_spin_coords_persisted_to_slot_memory(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=10.0, spin_button_coords=None),
            _ocr_state(saldo=90.0, posta=10.0, spin_button_coords=None),
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10, slot_name="Big Bass")

        assert ok is True
        assert load_slot_memory("Big Bass")["known_spin_coords"] == (540, 1200)

    def test_no_memory_used_or_written_without_slot_name(self, mocks, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocks["ocr_reader"].read_slot_state.side_effect = [
            _ocr_state(saldo=100.0, posta=10.0, spin_button_coords=None),
            _ocr_state(saldo=90.0, posta=10.0, spin_button_coords=None),
        ]
        mocks["engine"].ask_with_image.side_effect = [
            Result.ok(_answers("tap", _target(region="center"))),
            Result.ok(_answers("stop")),
        ]

        ok = run_autonomous_session("dev", target_amount=30.0, max_steps=10)

        assert ok is True
        assert not os.path.exists(SLOT_MEMORY_DIR)

    def test_hints_persisted_when_bet_successfully_lowered(self, mocks, tmp_path, monkeypatch):
        """Un suggerimento attivo nel momento in cui la posta scende al minimo
        viene considerato confermato utile e salvato per la prossima volta che
        si gioca questa slot — questo e' il modo in cui il sistema 'si allena'."""
        monkeypatch.chdir(tmp_path)
        state = {"posta": 10.0}

        def ocr_side_effect(*_a, **_k):
            return _ocr_state(saldo=100.0, posta=state["posta"], spin_button_coords=(540, 1200))

        mocks["ocr_reader"].read_slot_state.side_effect = ocr_side_effect

        def ask_human_help_side_effect(*_a, **_k):
            state["posta"] = 0.10  # il suggerimento, una volta seguito da GPT, abbassa la posta
            return ("text", "tap coins icon")

        regions = ["top-left", "top-right", "bottom-left", "bottom-right", "middle-left", "top-center"]
        call_count = {"n": 0}

        def ask_side_effect(*_a, **_k):
            call_count["n"] += 1
            if call_count["n"] <= len(regions):
                return Result.ok(_answers("tap", _target(region=regions[call_count["n"] - 1])))
            return Result.ok(_answers("stop"))

        mocks["engine"].ask_with_image.side_effect = ask_side_effect

        with patch(
            "orchestrator.tests.validation.test_slot_autonomous.ask_human_help",
            side_effect=ask_human_help_side_effect,
        ):
            ok = run_autonomous_session("dev", target_amount=30.0, max_steps=20, slot_name="Big Bass 2")

        assert ok is True
        assert "tap coins icon" in load_slot_memory("Big Bass 2")["learned_hints"]
