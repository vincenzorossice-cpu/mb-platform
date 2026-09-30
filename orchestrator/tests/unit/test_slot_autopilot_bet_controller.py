from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.provider_profile.autopilot_memory import load_autopilot_flag, save_autopilot_flag
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.bet_menu import MenuSelection
from orchestrator.modules.slot_autopilot.screen_recovery import RecoveryOutcome
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.strategies.base import LocateCandidate, LocateContext
from orchestrator.modules.slot_autopilot.strategies.profile_strategy import ProfileStrategy
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier, TapOutcome
from orchestrator.tests.unit._fake_slot_device import (
    FakeSlotDevice,
    fake_ocr_reader,
    fake_screen_reader,
    fake_touch_injector,
)


def _config(**overrides) -> AutoPilotConfig:
    defaults = dict(
        device_id="dev", slot_name="pirots5", target_played_amount=Decimal("30.00"),
        stable_reads_for_min=3, max_bet_lower_attempts=20,
        frame_diff_poll_interval=0.01, frame_diff_max_wait_seconds=1.0,
        bet_burst_taps=1, bet_burst_interval_seconds=0.0,   # raffica spenta di default: ogni test la accende se serve
    )
    defaults.update(overrides)
    return AutoPilotConfig(**defaults)


def _context_provider(initial_bet=Decimal("1.00")):
    context = LocateContext(
        device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
        screen_width=1080, screen_height=2400, slot_ocr_state={"posta": float(initial_bet)},
    )
    return MagicMock(return_value=Result.ok(context))


def _ok_candidate(strategy_name="ocr_anchor"):
    return Result.ok(LocateCandidate(x=200, y=2200, confidence=0.75, strategy_name=strategy_name))


def _tap_outcome(bet_before, bet_after, verified, outcome):
    return TapOutcome(
        tapped_coords=(200, 2200), verified=verified, screenshot_before="/tmp/a.png",
        screenshot_after="/tmp/b.png", diff_score=0.5,
        balance_before=Decimal("10.00"), balance_after=Decimal("10.00"),
        bet_before=bet_before, bet_after=bet_after,
        screen_state_before="idle", screen_state_after="idle", outcome=outcome,
    )


def _locator_mock():
    """MagicMock generico per il locator con is_exhausted()/is_trusted_strategy()
    gia' impostati (rispettivamente False e False, coerente col default
    "ocr_anchor" non fidato di _ok_candidate()): senza questo, MagicMock()
    restituirebbe un Mock (sempre truthy) per qualunque chiamata non
    configurata, facendo scattare per errore il nuovo ramo "strategia
    esaurita" in OGNI test che non lo sta esercitando di proposito."""
    locator = MagicMock()
    locator.is_exhausted.return_value = False
    locator.is_trusted_strategy.return_value = False
    return locator


def _controller(locator, verifier, config=None, step_logger=None):
    step_logger = step_logger or MagicMock()
    step_logger.log_step.return_value = Result.ok(True)
    return BetController(locator, verifier, step_logger, config or _config()), step_logger


class TestSetMinBetWithMockedCollaborators:
    def test_converges_to_minimum_after_plateau(self):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.20"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.10"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect")),
        ]
        controller, step_logger = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        bet_result = result.data
        assert bet_result.final_bet == Decimal("0.10")
        assert bet_result.min_bet_verified is True
        assert bet_result.outcome == "min_bet_confirmed"
        assert step_logger.log_step.call_count == 6
        assert locator.save_verified_candidate.call_count == 3

    def test_progress_is_logged(self, caplog):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")
        )
        controller, _ = _controller(locator, verifier, config=_config(stable_reads_for_min=1))

        with caplog.at_level("INFO"):
            controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        messages = [record.getMessage() for record in caplog.records]
        assert any("puntata iniziale" in m for m in messages)
        assert any("tentativo" in m for m in messages)
        assert any("plateau raggiunto" in m for m in messages)

    def test_plateau_without_any_decrease_and_without_a_trusted_profile_is_not_verified(self):
        """Senza un profilo verificato che confermi il minimo, un plateau
        senza MAI una discesa vera non puo' essere fidato: potremmo essere
        gia' al minimo, oppure star tappando il posto sbagliato (es. le
        schede di Chrome) - non possiamo distinguere i due casi, quindi non
        si procede a spinnare su un'ipotesi mai verificata."""
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.min_bet_verified is False
        assert result.data.aborted_reason == "NO_DECREASE_OBSERVED"
        assert result.data.final_bet == Decimal("1.00")
        assert result.data.attempts == 3

    def test_starting_bet_already_at_verified_profile_min_bet_skips_everything(self):
        """Se la puntata di partenza combacia gia' col known_min_bet di un
        profilo verificato, il riscontro incrociato lo riconosce SUBITO: non
        si tenta nemmeno di localizzare/tappare bet_minus (e quindi non si
        corre alcun rischio, es. GPT che sceglie un punto sbagliato)."""
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
        )
        context_provider = MagicMock(return_value=Result.ok(LocateContext(
            device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
            screen_width=1080, screen_height=2400, slot_ocr_state={"posta": 0.20},
            profile=profile,
        )))
        locator = _locator_mock()
        verifier = MagicMock()
        controller, step_logger = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.min_bet_verified is True
        assert result.data.aborted_reason is None
        assert result.data.final_bet == Decimal("0.20")
        assert result.data.attempts == 0
        locator.locate.assert_not_called()
        verifier.verified_tap.assert_not_called()
        step_logger.log_step.assert_not_called()

    def test_plateau_without_decrease_and_profile_min_bet_mismatch_is_not_trusted(self):
        """Il profilo dice che il minimo e' 0.20, ma il plateau si e' fermato
        a 1.00: il riscontro incrociato NON conferma, quindi non ci si fida
        (esattamente il caso in cui GPT potrebbe aver tappato il posto
        sbagliato mentre la vera puntata minima resta piu' bassa)."""
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
        )
        context_provider = MagicMock(return_value=Result.ok(LocateContext(
            device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
            screen_width=1080, screen_height=2400, slot_ocr_state={"posta": 1.00},
            profile=profile,
        )))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert result.data.min_bet_verified is False
        assert result.data.aborted_reason == "NO_DECREASE_OBSERVED"

    def test_bet_rising_aborts_immediately(self):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("2.00"), False, "unexpected_change")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "VALUE_ROSE"
        assert result.data.outcome == "aborted"
        assert locator.locate.call_count == 1
        assert verifier.verified_tap.call_count == 1

    def test_hitl_stop_mid_loop_aborts(self):
        locator = _locator_mock()
        locator.locate.side_effect = [
            _ok_candidate(), _ok_candidate(),
            Result.fail("operator stopped", "HITL_STOP_REQUESTED"),
        ]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.20"), True, "success")),
        ]
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "HITL_STOP"
        assert result.data.attempts == 3

    def test_generic_locate_failure_is_soft_and_exhausts_to_max_attempts(self):
        """Comportamento cambiato deliberatamente: un fallimento generico di
        locate() non abortisce piu' al primo colpo (solo HITL_STOP_REQUESTED
        lo fa) - riprova fino al limite configurato."""
        locator = _locator_mock()
        locator.locate.return_value = Result.fail("nothing found", "ELEMENT_NOT_LOCATABLE")
        verifier = MagicMock()
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=2))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.attempts == 2
        assert locator.locate.call_count == 2
        verifier.verified_tap.assert_not_called()

    def test_hitl_hint_only_does_not_abort_immediately(self):
        """L'operatore ha gia' interagito utilmente (es. aperto il menu
        giusto) ma la puntata non e' ancora scesa: HITL_HINT_ONLY non deve
        fermare tutto al primo scambio, deve dare una seconda possibilita'."""
        locator = _locator_mock()
        locator.locate.side_effect = [
            Result.fail("screen changed, hint only", "HITL_HINT_ONLY"),
            Result.fail("screen changed, hint only", "HITL_HINT_ONLY"),
        ]
        verifier = MagicMock()
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=2))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.attempts == 2
        assert locator.locate.call_count == 2
        verifier.verified_tap.assert_not_called()

    def test_hitl_skipped_does_not_abort_immediately(self):
        locator = _locator_mock()
        locator.locate.side_effect = [
            Result.fail("operator skipped", "HITL_SKIPPED"),
            Result.fail("operator skipped", "HITL_SKIPPED"),
        ]
        verifier = MagicMock()
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=2))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.attempts == 2

    def test_recovers_after_a_soft_failure_then_succeeds(self):
        """Dimostra che l'interazione HITL a piu' scambi ora funziona: un
        primo 'hint' seguito da tentativi che trovano e abbassano davvero la
        puntata converge comunque al minimo, invece di fermarsi al primo
        scambio."""
        locator = _locator_mock()
        locator.locate.side_effect = [
            Result.fail("screen changed, hint only", "HITL_HINT_ONLY"),
            _ok_candidate(),
            _ok_candidate(),
            _ok_candidate(),
        ]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), False, "no_effect")),
        ]
        controller, _ = _controller(locator, verifier, config=_config(stable_reads_for_min=2))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.min_bet_verified is True
        assert result.data.outcome == "min_bet_confirmed"
        assert result.data.final_bet == Decimal("0.50")
        assert result.data.attempts == 4

    def test_no_effect_from_an_exhausted_strategy_is_not_a_stable_read(self):
        """Riproduce l'incidente reale: una strategia non fidata (es. 'gpt')
        restituisce sempre la STESSA coordinata inerte, tap sempre
        'no_effect'. Prima del fix, 3 letture 'no_effect' di fila bastavano
        a dichiarare plateau/gia'-al-minimo anche se la strategia era ormai
        esaurita (e quindi al prossimo locate() la catena sarebbe scesa su
        un'altra, es. hitl). Ora una lettura da una strategia esaurita non
        conta come prova di plateau: il sistema continua a riprovare, senza
        mai dichiarare un falso 'gia' al minimo'."""
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        locator.is_exhausted.side_effect = [False, False, True, True, True]
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=5))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider(Decimal("6.00")))
        assert result.success
        assert result.data.outcome == "aborted"
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.min_bet_verified is False
        assert result.data.attempts == 5
        assert locator.record_failure.call_count == 5

    def test_recovers_by_switching_strategy_after_one_is_exhausted(self):
        """Nella realta', dopo che 'gpt' si esaurisce, il prossimo locate()
        scende automaticamente alla strategia successiva nella catena (fino
        a hitl). Qui lo simuliamo direttamente: 3 tap inerti da 'gpt', poi
        una strategia diversa che decrementa davvero - il plateau finale
        riflette il minimo VERO, non quello inventato da gpt."""
        locator = _locator_mock()
        locator.locate.side_effect = [
            _ok_candidate("gpt"), _ok_candidate("gpt"), _ok_candidate("gpt"),
            _ok_candidate("hitl"), _ok_candidate("hitl"), _ok_candidate("hitl"), _ok_candidate("hitl"),
        ]
        locator.is_exhausted.side_effect = [False, False, True, False, False, False]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("0.20"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.20"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.20"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.20"), False, "no_effect")),
        ]
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=10))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider(Decimal("6.00")))
        assert result.success
        assert result.data.outcome == "min_bet_confirmed"
        assert result.data.min_bet_verified is True
        assert result.data.final_bet == Decimal("0.20")
        assert result.data.attempts == 7
        assert locator.save_verified_candidate.call_count == 1

    def test_trusted_strategy_no_effect_still_counts_as_a_stable_read(self):
        """Una strategia fidata (profile/memory/hitl) che smette di far
        scendere la puntata e' proprio la firma di un plateau vero, non di
        un bersaglio sbagliato: a differenza di una strategia non fidata, il
        conteggio verso stable_reads non viene mai sospeso, e il meccanismo
        record_failure/is_exhausted non viene nemmeno interpellato."""
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("hitl")
        locator.is_trusted_strategy.return_value = True
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.attempts == 3
        locator.is_exhausted.assert_not_called()
        locator.record_failure.assert_not_called()

    def test_max_attempts_exhausted(self):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")
        )
        config = _config(max_bet_lower_attempts=3, stable_reads_for_min=100)
        controller, _ = _controller(locator, verifier, config=config)

        result = controller.set_min_bet("dev", "pirots5", "ep1", _context_provider())
        assert result.success
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.attempts == 3


class TestBetMenuToggleFallback:
    """Quando il profilo curato dice che questa slot non ha un pulsante '-'
    ma ha un'icona che apre/chiude il menu puntata (es. Pirots5), il sistema
    deve usarla da solo invece di far scoprire la stessa cosa a un umano via
    HITL al primo fallimento di locate('bet_minus')."""

    def _profile_without_bet_minus(self, verified=True, known_min_bet=0.20):
        return ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=verified, known_min_bet=known_min_bet,
            elements={"bet_menu_toggle": ElementSpec(name="bet_menu_toggle", norm_x=0.5, norm_y=0.9)},
        )

    def _context_with_profile(self, profile, bet=Decimal("6.00")):
        context = LocateContext(
            device_id="dev", slot_name="pirots5", screenshot_path="/tmp/x.png",
            screen_width=1080, screen_height=2400, slot_ocr_state={"posta": float(bet)},
            profile=profile,
        )
        return MagicMock(return_value=Result.ok(context))

    def test_opens_menu_proactively_when_profile_has_toggle_but_no_bet_minus(self):
        profile = self._profile_without_bet_minus()
        context_provider = self._context_with_profile(profile)
        locator = _locator_mock()
        locator.locate.side_effect = [
            _ok_candidate("profile"),  # bet_menu_toggle
            _ok_candidate("gpt"),  # bet_minus, riprende normalmente dopo
            _ok_candidate("profile"),  # bet_menu_toggle, per richiudere il menu a fine tentativi
        ]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), True, "success")),  # apertura menu
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")),  # bet_minus
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), True, "success")),  # chiusura menu
        ]
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=1, stable_reads_for_min=100))

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert locator.locate.call_count == 3   # toggle, bet_minus, toggle per richiudere il menu
        assert locator.locate.call_args_list[0].args[0] == "bet_menu_toggle"
        assert locator.locate.call_args_list[1].args[0] == "bet_minus"
        assert locator.locate.call_args_list[2].args[0] == "bet_menu_toggle"

    def test_skips_proactive_open_when_bet_minus_is_documented(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
            elements={
                "bet_minus": ElementSpec(name="bet_minus", norm_x=0.1, norm_y=0.9),
                "bet_menu_toggle": ElementSpec(name="bet_menu_toggle", norm_x=0.5, norm_y=0.9),
            },
        )
        context_provider = self._context_with_profile(profile)
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("profile")
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=1, stable_reads_for_min=100))

        controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert locator.locate.call_count == 1
        assert locator.locate.call_args_list[0].args[0] == "bet_minus"

    def test_skips_proactive_open_when_profile_is_not_verified(self):
        profile = self._profile_without_bet_minus(verified=False)
        context_provider = self._context_with_profile(profile)
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=1, stable_reads_for_min=100))

        controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert locator.locate.call_count == 1
        assert locator.locate.call_args_list[0].args[0] == "bet_minus"

    def test_proactive_open_failure_does_not_block_the_loop(self):
        profile = self._profile_without_bet_minus()
        context_provider = self._context_with_profile(profile)
        locator = _locator_mock()
        locator.locate.side_effect = [
            Result.fail("not found", "ELEMENT_NOT_LOCATABLE"),  # bet_menu_toggle non localizzabile
            _ok_candidate("gpt"),  # bet_minus prosegue comunque
        ]
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("6.00"), Decimal("6.00"), False, "no_effect")
        )
        controller, _ = _controller(locator, verifier, config=_config(max_bet_lower_attempts=1, stable_reads_for_min=100))

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert locator.locate.call_count == 2
        verifier.verified_tap.assert_called_once()

    def test_stops_immediately_when_a_decrease_lands_exactly_on_the_known_minimum(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
        )
        context_provider = self._context_with_profile(profile, bet=Decimal("1.00"))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("0.20"), True, "success")
        )
        controller, _ = _controller(locator, verifier, config=_config(stable_reads_for_min=3, max_bet_lower_attempts=20))

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert result.data.outcome == "min_bet_confirmed"
        assert result.data.min_bet_verified is True
        assert result.data.attempts == 1
        assert result.data.final_bet == Decimal("0.20")
        assert locator.locate.call_count == 1

    def test_does_not_stop_immediately_when_still_above_the_known_minimum(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
        )
        context_provider = self._context_with_profile(profile, bet=Decimal("1.00"))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")
        )
        controller, _ = _controller(locator, verifier, config=_config(stable_reads_for_min=3, max_bet_lower_attempts=1))

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert result.data.outcome == "aborted"
        assert result.data.aborted_reason == "MAX_ATTEMPTS"
        assert result.data.final_bet == Decimal("0.50")

    def test_closes_the_menu_after_reaching_a_confirmed_minimum_if_it_opened_it(self):
        profile = self._profile_without_bet_minus()
        context_provider = self._context_with_profile(profile)
        locator = _locator_mock()
        locator.locate.side_effect = [
            _ok_candidate("profile"),  # apertura
            _ok_candidate("gpt"),      # bet_minus -> arriva subito al minimo
            _ok_candidate("profile"),  # richiusura
        ]
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("6.00"), True, "success")),
            Result.ok(_tap_outcome(Decimal("6.00"), Decimal("0.20"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.20"), Decimal("0.20"), True, "success")),
        ]
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert result.data.outcome == "min_bet_confirmed"
        assert locator.locate.call_count == 3
        assert locator.locate.call_args_list[2].args[0] == "bet_menu_toggle"
        assert verifier.verified_tap.call_count == 3

    def test_does_not_attempt_to_close_when_it_never_opened_the_menu(self):
        profile = ProviderProfile(
            slot_name="pirots5", provider="hacksaw", screen_width=1080, screen_height=2400,
            verified=True, known_min_bet=0.20,
            elements={"bet_minus": ElementSpec(name="bet_minus", norm_x=0.1, norm_y=0.9)},
        )
        context_provider = self._context_with_profile(profile, bet=Decimal("1.00"))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("profile")
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(
            _tap_outcome(Decimal("1.00"), Decimal("0.20"), True, "success")
        )
        controller, _ = _controller(locator, verifier)

        result = controller.set_min_bet("dev", "pirots5", "ep1", context_provider)
        assert result.success
        assert locator.locate.call_count == 1


class TestEndToEndWithFakeSlotDevice:
    def test_converges_to_true_minimum(self, tmp_path):
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        ladder = [Decimal("0.50"), Decimal("0.20"), Decimal("0.10")]
        state = {"i": 0}

        def _on_tap(d):
            if state["i"] < len(ladder):
                d.bet = ladder[state["i"]]
                state["i"] += 1

        device.add_element("bet_minus", 200, 2200, _on_tap)
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=device.width, screen_height=device.height,
            elements={"bet_minus": ElementSpec(
                name="bet_minus", norm_x=200 / device.width, norm_y=2200 / device.height,
            )},
        )
        locator = ElementLocator({"profile": ProfileStrategy(ProviderProfileLoader())})
        verifier = ActionVerifier(
            fake_touch_injector(device), fake_screen_reader(device), fake_ocr_reader(device), _config(),
        )
        controller, _ = _controller(locator, verifier, config=_config())

        def _provider():
            return Result.ok(LocateContext(
                device_id="dev", slot_name="pirots5", screenshot_path=device.screenshot(),
                screen_width=device.width, screen_height=device.height,
                slot_ocr_state=device.ocr_state(), profile=profile,
            ))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _provider)
        assert result.success
        assert result.data.final_bet == Decimal("0.10")
        assert result.data.min_bet_verified is True
        assert result.data.outcome == "min_bet_confirmed"

    def test_dead_zone_profile_coords_produce_false_floor(self, tmp_path):
        """Limite noto e dichiarato nel piano: coordinate del profilo su una
        zona morta non fanno mai scendere la catena a Memory/OCR/GPT/HITL
        perche' localizzare e avere-effetto sono passi distinti per design.
        Il plateau che ne risulta pero' NON viene piu' marcato verificato
        (nessuna discesa vera osservata, nessun known_min_bet a riscontro nel
        profilo di questo test): l'esito resta reso sicuro da Fix 2, non solo
        documentato."""
        device = FakeSlotDevice(tmp_path, balance=Decimal("10.00"), bet=Decimal("1.00"))
        # Nessun elemento registrato in quella posizione: sempre zona morta.
        profile = ProviderProfile(
            slot_name="pirots5", provider="x", screen_width=device.width, screen_height=device.height,
            elements={"bet_minus": ElementSpec(name="bet_minus", norm_x=0.01, norm_y=0.01)},
        )
        locator = ElementLocator({"profile": ProfileStrategy(ProviderProfileLoader())})
        verifier = ActionVerifier(
            fake_touch_injector(device), fake_screen_reader(device), fake_ocr_reader(device), _config(),
        )
        controller, _ = _controller(locator, verifier, config=_config())

        def _provider():
            return Result.ok(LocateContext(
                device_id="dev", slot_name="pirots5", screenshot_path=device.screenshot(),
                screen_width=device.width, screen_height=device.height,
                slot_ocr_state=device.ocr_state(), profile=profile,
            ))

        result = controller.set_min_bet("dev", "pirots5", "ep1", _provider)
        assert result.success
        assert result.data.outcome == "unchanged_already_min"
        assert result.data.final_bet == Decimal("1.00")
        assert result.data.min_bet_verified is False
        assert result.data.aborted_reason == "NO_DECREASE_OBSERVED"


class TestMenuFallback:
    def _exhausting_setup(self, tmp_path, selector_result, flag=False):
        config = _config(slot_memory_dir=str(tmp_path), max_bet_lower_attempts=6)
        if flag:
            save_autopilot_flag("pirots5", "bet_via_menu", True, str(tmp_path))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        locator.is_exhausted.side_effect = [False, False, True] + [True] * 10
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(_tap_outcome(Decimal("2.00"), Decimal("2.00"), False, "no_effect"))
        selector = MagicMock()
        selector.select_lowest.return_value = selector_result
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = BetController(locator, verifier, step_logger, config, menu_selector=selector)
        return controller, selector, config

    def _selection(self):
        return Result.ok(MenuSelection(final_bet=Decimal("0.10"), lowest_listed=Decimal("0.10"), toggle_strategy="gpt"))

    def test_menu_is_tried_when_the_minus_button_strategy_is_exhausted(self, tmp_path):
        controller, selector, config = self._exhausting_setup(tmp_path, self._selection())

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.10")
        assert result.data.strategy_sequence[-2:] == ["gpt", "menu"]
        selector.select_lowest.assert_called_once()
        assert load_autopilot_flag("pirots5", "bet_via_menu", str(tmp_path))

    def test_failed_menu_falls_through_and_is_tried_only_once(self, tmp_path):
        controller, selector, _ = self._exhausting_setup(tmp_path, Result.fail("x", "MENU_NOT_READABLE"))

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert not result.data.min_bet_verified
        selector.select_lowest.assert_called_once()
        assert not load_autopilot_flag("pirots5", "bet_via_menu", str(tmp_path))

    def test_remembered_menu_route_goes_first_without_any_minus_attempt(self, tmp_path):
        controller, selector, _ = self._exhausting_setup(tmp_path, self._selection(), flag=True)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.min_bet_verified and result.data.attempts == 1
        controller.verifier.verified_tap.assert_not_called()
        controller.locator.locate.assert_not_called()

    def test_without_a_selector_nothing_changes(self, tmp_path):
        config = _config(slot_memory_dir=str(tmp_path), max_bet_lower_attempts=4)
        save_autopilot_flag("pirots5", "bet_via_menu", True, str(tmp_path))
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate()
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success"))
        controller, _ = _controller(locator, verifier, config=_config(slot_memory_dir=str(tmp_path), stable_reads_for_min=1))

        assert controller.set_min_bet("dev", "pirots5", "ep", _context_provider()).success
        verifier.verified_tap.assert_called()


class TestRecoveryFromUnexpectedScreen:
    def _run(self, recovery_result, taps):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        verifier = MagicMock()
        verifier.verified_tap.side_effect = taps
        recovery = MagicMock()
        recovery.recover.return_value = recovery_result
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = BetController(
            locator, verifier, step_logger, _config(stable_reads_for_min=1), screen_recovery=recovery,
        )
        return controller, locator, recovery

    def test_unexpected_change_rejects_the_point_recovers_and_continues(self):
        taps = [
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("10.0"), False, "unexpected_change")),
            Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.05"), True, "success")),
            Result.ok(_tap_outcome(Decimal("0.05"), Decimal("0.05"), False, "no_effect")),
        ]
        controller, locator, recovery = self._run(Result.ok(RecoveryOutcome("ocr_close", 1)), taps)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

        locator.reject_position.assert_called_once_with("bet_minus", 200, 2200)
        recovery.recover.assert_called_once()
        assert recovery.recover.call_args.args[1] == "/tmp/a.png"   # screenshot_before del tap sbagliato
        # la lettura "10.0" della schermata sbagliata NON ha fatto scattare VALUE_ROSE
        assert result.data.aborted_reason != "VALUE_ROSE"
        assert result.data.final_bet == Decimal("0.05")

    def test_failed_recovery_stops_without_any_further_tap(self):
        taps = [Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "unexpected_change"))]
        controller, _, _ = self._run(Result.fail("stuck", "RECOVERY_FAILED"), taps)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

        assert result.data.aborted_reason == "STUCK_ON_UNEXPECTED_SCREEN"
        assert not result.data.min_bet_verified
        assert controller.verifier.verified_tap.call_count == 1

    def test_no_effect_does_not_trigger_recovery(self):
        taps = [Result.ok(_tap_outcome(Decimal("0.10"), Decimal("0.10"), False, "no_effect"))] * 5
        controller, locator, recovery = self._run(Result.ok(RecoveryOutcome("x", 0)), taps)

        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("0.10")))

        recovery.recover.assert_not_called()
        locator.reject_position.assert_not_called()


class TestPermanentRejectionAfterRecovery:
    def _run(self, method):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("2.00"), Decimal("10.00"), False, "unexpected_change")),
            Result.ok(_tap_outcome(Decimal("2.00"), Decimal("1.00"), True, "success")),
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
        ]
        recovery = MagicMock()
        recovery.recover.return_value = Result.ok(RecoveryOutcome(method, 1))
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = BetController(
            locator, verifier, step_logger, _config(stable_reads_for_min=1), screen_recovery=recovery,
        )
        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))
        return locator, recovery

    def test_undone_tap_is_remembered_for_good(self):
        locator, recovery = self._run("retap")

        locator.remember_rejected.assert_called_once_with("pirots5", "bet_minus", 200, 2200)
        assert recovery.recover.call_args.args[4] == {"posta": Decimal("2.00"), "saldo": Decimal("10.00")}

    def test_nothing_to_undo_is_not_remembered_for_good(self):
        locator, _ = self._run("already_original")

        locator.remember_rejected.assert_not_called()
        locator.reject_position.assert_called_once()


class TestMenuStuckAborts:
    def test_a_stuck_menu_attempt_aborts_without_further_taps(self, tmp_path):
        config = _config(slot_memory_dir=str(tmp_path), max_bet_lower_attempts=6)
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        locator.is_exhausted.side_effect = [False, False, True] + [True] * 10
        verifier = MagicMock()
        verifier.verified_tap.return_value = Result.ok(_tap_outcome(Decimal("2.00"), Decimal("2.00"), False, "no_effect"))
        selector = MagicMock()
        selector.select_lowest.return_value = Result.fail("stuck", "MENU_STUCK")
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        controller = BetController(locator, verifier, step_logger, config, menu_selector=selector)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.aborted_reason == "STUCK_ON_UNEXPECTED_SCREEN"
        assert verifier.verified_tap.call_count == 3   # nessun tocco dopo il menu


class TestSteppersMenuFlow:
    """Menu a righe con pulsanti -/+ (non una lista): aperto dal selettore, poi
    il solito ciclo dei pulsanti meno dentro il menu, fino al plateau."""

    def _setup(self, tmp_path):
        config = _config(slot_memory_dir=str(tmp_path), max_bet_lower_attempts=12, stable_reads_for_min=3)
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate("gpt")
        locator.is_exhausted.side_effect = [False, False, True] + [False] * 30
        selector = MagicMock()
        selector.select_lowest.return_value = Result.fail("not a list", "MENU_OPEN_NOT_A_LIST")
        step_logger = MagicMock()
        step_logger.log_step.return_value = Result.ok(True)
        return locator, selector, step_logger, config

    def test_menu_left_open_continues_with_minus_presses_inside_the_menu(self, tmp_path):
        locator, selector, step_logger, config = self._setup(tmp_path)
        verifier = MagicMock()
        verifier.verified_tap.side_effect = (
            [Result.ok(_tap_outcome(Decimal("2.00"), Decimal("2.00"), False, "no_effect"))] * 3   # - sulla schermata principale
            + [Result.ok(_tap_outcome(Decimal("2.00"), Decimal("1.00"), True, "success")),       # - dentro il menu
               Result.ok(_tap_outcome(Decimal("1.00"), Decimal("0.50"), True, "success")),
               Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("0.50"), Decimal("0.50"), True, "success"))]         # chiusura menu
        )
        controller = BetController(locator, verifier, step_logger, config, menu_selector=selector)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.min_bet_verified and result.data.final_bet == Decimal("0.50")
        selector.select_lowest.assert_called_once()
        locator.reset_failures.assert_called_with("bet_minus")   # il menu e' un contesto nuovo: GPT riparte da zero
        assert verifier.verified_tap.call_count == 9   # ...+ richiusura del menu al plateau

    def test_the_menu_route_is_remembered_after_a_verified_minimum(self, tmp_path):
        locator, selector, step_logger, config = self._setup(tmp_path)
        verifier = MagicMock()
        verifier.verified_tap.side_effect = (
            [Result.ok(_tap_outcome(Decimal("2.00"), Decimal("2.00"), False, "no_effect"))] * 3
            + [Result.ok(_tap_outcome(Decimal("2.00"), Decimal("1.00"), True, "success")),
               Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
               Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), True, "success"))]
        )
        BetController(locator, verifier, step_logger, config, menu_selector=selector).set_min_bet(
            "dev", "pirots5", "ep", _context_provider(Decimal("2.00")),
        )

        assert load_autopilot_flag("pirots5", "bet_via_menu", str(tmp_path))

    def test_a_remembered_menu_route_opens_the_menu_first(self, tmp_path):
        locator, selector, step_logger, config = self._setup(tmp_path)
        save_autopilot_flag("pirots5", "bet_via_menu", True, str(tmp_path))
        locator.is_exhausted.side_effect = None
        locator.is_exhausted.return_value = False
        verifier = MagicMock()
        verifier.verified_tap.side_effect = [
            Result.ok(_tap_outcome(Decimal("2.00"), Decimal("1.00"), True, "success")),
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), False, "no_effect")),
            Result.ok(_tap_outcome(Decimal("1.00"), Decimal("1.00"), True, "success")),
        ]
        controller = BetController(locator, verifier, step_logger, config, menu_selector=selector)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.min_bet_verified
        selector.select_lowest.assert_called_once()    # prima di qualunque tocco sul "-" esterno


def _decrease(before, after, **kw):
    outcome = _tap_outcome(Decimal(str(before)), Decimal(str(after)), True, "success")
    outcome.state_after = {"posta": float(after), "saldo": 100.0, "raw_texts": []}
    outcome.screenshot_after = "/tmp/after.png"
    return Result.ok(outcome)


def _flat(value):
    outcome = _tap_outcome(Decimal(str(value)), Decimal(str(value)), False, "no_effect")
    outcome.state_after = {"posta": float(value), "saldo": 100.0, "raw_texts": []}
    outcome.screenshot_after = "/tmp/after.png"
    return Result.ok(outcome)


class TestFasterDescent:
    def _controller_for(self, taps, strategy="memory", trusted=True, **config):
        locator = _locator_mock()
        locator.locate.return_value = _ok_candidate(strategy)
        locator.is_trusted_strategy.return_value = trusted
        verifier = MagicMock()
        verifier.verified_tap.side_effect = taps
        controller, step_logger = _controller(locator, verifier, config=_config(**config))
        return controller, locator, verifier

    def test_the_before_reading_comes_from_the_context_not_from_a_new_ocr(self):
        controller, _, verifier = self._controller_for([_decrease(2, 1), _flat(1), _flat(1), _flat(1)])
        provider = _context_provider(Decimal("2.00"))

        controller.set_min_bet("dev", "pirots5", "ep", provider)

        first = verifier.verified_tap.call_args_list[0].kwargs
        assert first["screenshot_before"] == "/tmp/x.png" and first["state_before"] == {"posta": 2.0}

    def test_a_trusted_tap_outcome_becomes_the_next_context_without_rereading_the_screen(self):
        controller, _, verifier = self._controller_for([_decrease(2, 1), _flat(1), _flat(1), _flat(1)])
        provider = _context_provider(Decimal("2.00"))

        controller.set_min_bet("dev", "pirots5", "ep", provider)

        assert provider.call_count == 1      # solo la lettura iniziale: niente OCR tra un tocco e l'altro
        second = verifier.verified_tap.call_args_list[1].kwargs
        assert second["screenshot_before"] == "/tmp/after.png" and second["state_before"]["posta"] == 1.0

    def test_an_untrusted_strategy_still_rereads_the_screen(self):
        controller, _, verifier = self._controller_for(
            [_decrease(2, 1), _flat(1), _flat(1), _flat(1)], strategy="gpt", trusted=False,
        )
        provider = _context_provider(Decimal("2.00"))

        controller.set_min_bet("dev", "pirots5", "ep", provider)

        assert provider.call_count > 1

    def test_burst_after_two_consecutive_decreases_then_one_verified_tap(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_controller.time.sleep", sleeps.append)
        taps = [_decrease(2.0, 1.8), _decrease(1.8, 1.6), _decrease(1.6, 1.0), _flat(1.0), _flat(1.0), _flat(1.0)]
        controller, _, verifier = self._controller_for(taps, bet_burst_taps=4, bet_burst_interval_seconds=0.5)

        result = controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        assert result.data.min_bet_verified and result.data.final_bet == Decimal("1.0")
        blind = verifier.touch_injector.tap_coordinates.call_args_list
        # N-1 tocchi ciechi a ogni raffica: una dopo le 2 discese, una ancora perche' la 3a era scesa
        # (al minimo vero quella raffica non porta a nulla e si torna ai tocchi singoli)
        assert len(blind) == 6 and sleeps == [0.5] * 6
        third = verifier.verified_tap.call_args_list[2].kwargs
        assert third["state_before"]["posta"] == 1.6                  # il "prima" precede la raffica
        assert verifier.verified_tap.call_count == 6                  # le altre iterazioni restano singole

    def test_no_burst_with_an_untrusted_strategy(self):
        taps = [_decrease(2.0, 1.8), _decrease(1.8, 1.6), _decrease(1.6, 1.4), _flat(1.4), _flat(1.4), _flat(1.4)]
        controller, _, verifier = self._controller_for(taps, strategy="gpt", trusted=False, bet_burst_taps=4)

        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        verifier.touch_injector.tap_coordinates.assert_not_called()

    def test_burst_disabled_with_one_tap(self):
        taps = [_decrease(2.0, 1.8), _decrease(1.8, 1.6), _decrease(1.6, 1.4), _flat(1.4), _flat(1.4), _flat(1.4)]
        controller, _, verifier = self._controller_for(taps, bet_burst_taps=1)

        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        verifier.touch_injector.tap_coordinates.assert_not_called()

    def test_burst_that_does_not_lower_the_bet_falls_back_to_single_taps(self, monkeypatch):
        monkeypatch.setattr("orchestrator.modules.slot_autopilot.bet_controller.time.sleep", lambda s: None)
        taps = [_decrease(2.0, 1.8), _decrease(1.8, 1.6), _flat(1.6), _flat(1.6), _flat(1.6)]
        controller, _, verifier = self._controller_for(taps, bet_burst_taps=3)
        controller.set_min_bet("dev", "pirots5", "ep", _context_provider(Decimal("2.00")))

        # dopo la raffica a vuoto i tocchi tornano singoli: 1 sola raffica nonostante i 3 "flat"
        assert verifier.touch_injector.tap_coordinates.call_count == 2
