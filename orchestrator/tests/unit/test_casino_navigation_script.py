from orchestrator.tests.validation import test_casino_navigation as script


def test_goal_names_every_phase_and_forbids_gioca():
    goal = script.build_goal("Slot in Esclusiva", "Turbo Storm Typhoon")
    for text in ("Slot in Esclusiva", "Attiva la promozione", "Promozione attivata correttamente",
                 "Turbo Storm Typhoon", "'Prova'", "MAI 'Gioca'", "done"):
        assert text in goal


def test_defaults_match_the_requested_run():
    args = script.parse_args(["--demo"])
    assert (args.device_id, args.book, args.max_steps) == ("ZE223HNBKJ", "SISAL", 25)
    assert args.demo is True


def test_main_refuses_to_run_without_demo(monkeypatch):
    import pytest
    monkeypatch.setattr(script, "run_navigation", lambda args: pytest.fail("non deve partire"))
    with pytest.raises(SystemExit) as exit_info:
        script.main([])
    assert exit_info.value.code == 2
