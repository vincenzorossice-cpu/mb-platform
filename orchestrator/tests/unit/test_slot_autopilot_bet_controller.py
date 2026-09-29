from decimal import Decimal
from unittest.mock import MagicMock

from orchestrator.core.result import Result
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile import ElementSpec, ProviderProfile
from orchestrator.modules.slot_autopilot.bet_controller import BetController
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
