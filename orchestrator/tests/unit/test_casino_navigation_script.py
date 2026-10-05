import pytest
from orchestrator.tests.validation import test_casino_navigation as script


def test_goal_names_every_phase_and_forbids_gioca():
    goal = script.build_goal("Slot in Esclusiva", "Turbo Storm Typhoon")
    for text in ("Slot in Esclusiva", "Attiva la promozione", "Promozione attivata correttamente",
                 "Turbo Storm Typhoon", "'Prova'", "MAI 'Gioca'", "done"):
        assert text in goal


def test_defaults_match_the_requested_run():
    args = script.parse_args(["--demo"])
    assert (args.device_id, args.book, args.max_steps) == ("ZE223HNBKJ", "SISAL", 40)
    assert args.demo is True


def test_main_refuses_to_run_without_demo(monkeypatch):
    import pytest
    monkeypatch.setattr(script, "run_navigation", lambda args: pytest.fail("non deve partire"))
    with pytest.raises(SystemExit) as exit_info:
        script.main([])
    assert exit_info.value.code == 2


def test_log_file_and_step_screenshots_are_created(tmp_path):
    import logging
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = script.setup_logging("t", str(tmp_path))
        logging.getLogger("x").info("ciao")
        for handler in root.handlers:
            handler.flush()
        assert "ciao" in open(path, encoding="utf-8").read()
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()

    from PIL import Image
    shot = tmp_path / "s.png"
    Image.new("RGB", (10, 10)).save(shot)
    script.make_step_saver(str(tmp_path / "steps"))(3, str(shot))
    assert (tmp_path / "steps" / "step_03.png").exists()


def test_verdict_is_taken_from_the_operator_and_skipped_without_a_terminal():
    assert script.ask_verdict(lambda prompt: "s", interactive=True) is True
    assert script.ask_verdict(lambda prompt: "No", interactive=True) is False
    assert script.ask_verdict(lambda prompt: "", interactive=True) is None
    assert script.ask_verdict(lambda prompt: "s", interactive=False) is None


def test_the_run_record_carries_promo_and_slot():
    import inspect
    assert "promo=args.promo_name" in inspect.getsource(script.run_navigation)
    assert "slot=args.slot_name" in inspect.getsource(script.run_navigation)


class TestAlreadyActivatedPromo:
    def test_auto_goal_covers_both_cases(self):
        goal = script.build_goal("Happy Monday", "Slot X")
        assert "Promozioni attivate" in goal and "gia' attivata" in goal and "salta questa fase" in goal
        assert "Attiva la promozione" in goal and "Promozione attivata correttamente" in goal

    def test_already_active_goal_starts_from_activated_promotions_and_never_activates(self):
        goal = script.build_goal("Happy Monday", "Slot X", already_active=True)
        assert goal.index("Promozioni attivate") < goal.index("Slot X")
        assert "NON attivare nulla" in goal and "non andare avanti finche'" not in goal
        assert "MAI 'Gioca'" in goal and "'done' SOLO" in goal

    def test_not_active_goal_is_yesterdays_goal(self):
        goal = script.build_goal("Happy Monday", "Slot X", already_active=False)
        assert "non andare avanti finche' questo testo non e' leggibile" in goal
        assert "Promozioni attivate" not in goal

    def test_flags_map_to_modes_and_exclude_each_other(self):
        assert script.already_active_mode(script.parse_args(["--demo"])) is None
        assert script.already_active_mode(script.parse_args(["--demo", "--already-active"])) is True
        assert script.already_active_mode(script.parse_args(["--demo", "--not-active"])) is False
        with pytest.raises(SystemExit):
            script.parse_args(["--demo", "--already-active", "--not-active"])

    def test_the_run_record_carries_the_mode(self):
        import inspect
        assert "already_active=" in inspect.getsource(script.run_navigation)
