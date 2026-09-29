import argparse
import logging
import sys
from decimal import Decimal

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile_builder import ProfileBuilder
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.config import AutoPilotConfig
from orchestrator.modules.slot_autopilot.locator import ElementLocator
from orchestrator.modules.slot_autopilot.spin_controller import SpinController
from orchestrator.modules.slot_autopilot.strategies.gpt_locator_strategy import GPTLocatorStrategy
from orchestrator.modules.slot_autopilot.strategies.hitl_strategy import HITLStrategy
from orchestrator.modules.slot_autopilot.strategies.memory_strategy import MemoryStrategy
from orchestrator.modules.slot_autopilot.strategies.ocr_anchor_strategy import OCRAnchorStrategy
from orchestrator.modules.slot_autopilot.strategies.profile_strategy import ProfileStrategy
from orchestrator.modules.slot_autopilot.strategies.spatial_heuristic_strategy import SpatialHeuristicStrategy
from orchestrator.modules.slot_autopilot.strategies.template_strategy import TemplateStrategy
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier
from orchestrator.modules.slot_ocr.reader import SlotOCRReader
from orchestrator.modules.step_logger.logger import JSONLStepLogger
from orchestrator.modules.touch_injector.injector import TouchInjector

logger = logging.getLogger(__name__)


def build_autopilot(args: argparse.Namespace) -> Result[SlotAutoPilot]:
    """Unico punto del progetto dove queste classi concrete vengono
    istanziate direttamente: ogni modulo a monte le riceve per DI."""
    config = AutoPilotConfig(
        device_id=args.device, slot_name=args.slot, target_played_amount=Decimal(str(args.target)),
        max_steps=args.max_steps, enable_gpt_fallback=not args.no_gpt,
        enable_hitl_fallback=not args.no_hitl, dry_run=not getattr(args, "live", False),
        # Una slot nuova non ha ancora un profilo: deve poter partire e
        # impararlo (vedi ProfileBuilder). --require-profile ripristina il
        # blocco per chi vuole solo slot gia' curate.
        require_profile=getattr(args, "require_profile", False),
    )
    config_result = config.validate()
    if not config_result.success:
        return Result.fail(config_result.error, config_result.error_code)

    screen_reader = ScreenReader(adb_timeout=config.adb_timeout)
    touch_injector = TouchInjector(screen_reader=screen_reader, adb_timeout=config.adb_timeout)
    ocr_reader = SlotOCRReader()
    profile_loader = ProviderProfileLoader(profiles_dir=config.profiles_dir)
    step_logger = JSONLStepLogger(dataset_dir=config.dataset_dir)

    strategies = {
        "profile": ProfileStrategy(profile_loader),
        "memory": MemoryStrategy(memory_dir=config.slot_memory_dir),
        "ocr_anchor": OCRAnchorStrategy(),
        "spatial_heuristic": SpatialHeuristicStrategy(),
        "template": TemplateStrategy(),
    }

    if config.enable_gpt_fallback:
        try:
            gpt_client = GPTDecisionClient()
            strategies["gpt"] = GPTLocatorStrategy(gpt_client)
        except ValueError as exc:
            logger.warning("GPT fallback disabilitato (OPENAI_API_KEY mancante?): %s", exc)

    if config.enable_hitl_fallback:
        strategies["hitl"] = HITLStrategy(screen_reader, ocr_reader, touch_injector)

    locator = ElementLocator(strategies)
    verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, config)
    bet_controller = BetController(locator, verifier, step_logger, config)
    spin_controller = SpinController(locator, verifier, step_logger, config)

    autopilot = SlotAutoPilot(
        config=config, screen_reader=screen_reader, touch_injector=touch_injector,
        ocr_reader=ocr_reader, profile_loader=profile_loader, locator=locator,
        bet_controller=bet_controller, spin_controller=spin_controller, step_logger=step_logger,
        profile_builder=ProfileBuilder(profiles_dir=config.profiles_dir, memory_dir=config.slot_memory_dir),
    )
    return Result.ok(autopilot)


def _run_session(autopilot: SlotAutoPilot, args: argparse.Namespace) -> int:
    logger.info("Preparazione: profilo '%s', dispositivo %s...", args.slot, args.device)
    prepare_result = autopilot.prepare(args.device)
    if not prepare_result.success:
        logger.error("prepare() fallito: %s (%s)", prepare_result.error, prepare_result.error_code)
        return 1
    logger.info("Preparazione completata.")

    logger.info("Verifica puntata minima in corso...")
    bet_result = autopilot.set_min_bet(args.device)
    if not bet_result.success or not bet_result.data.min_bet_verified:
        reason = bet_result.data.aborted_reason if bet_result.success else bet_result.error_code
        logger.error("Impossibile verificare la puntata minima (%s): mi fermo senza spinnare.", reason)
        return 1
    logger.info(
        "Puntata minima verificata: %s EUR (outcome=%s, tentativi=%d)",
        bet_result.data.final_bet, bet_result.data.outcome, bet_result.data.attempts,
    )

    target = Decimal(str(args.target))
    total_played = Decimal("0.00")
    spin_index = 0
    while total_played < target and spin_index < args.max_steps:
        spin_index += 1
        spin_result = autopilot.spin(args.device, spin_index)
        if not spin_result.success:
            logger.error("spin() fallito: %s (%s)", spin_result.error, spin_result.error_code)
            return 1
        if spin_result.data.outcome == "success" and spin_result.data.bet_amount:
            total_played += spin_result.data.bet_amount
            logger.info("Progresso: %s/%s EUR giocati dopo lo spin %d", total_played, target, spin_index)
        elif spin_result.data.outcome == "blocked":
            logger.warning("Spin bloccato da un'invariante di sicurezza: %s", spin_result.data.violated_invariant)
            break
    logger.info("Sessione conclusa: %s/%s EUR giocati in %d spin.", total_played, target, spin_index)

    initial_balance, final_balance = autopilot.get_balance_summary()
    if initial_balance is not None and final_balance is not None:
        net = final_balance - initial_balance
        logger.info(
            "Riepilogo: saldo iniziale=%s, saldo finale=%s, netto=%s EUR",
            initial_balance, final_balance, net,
        )
    return 0


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SlotAutoPilot: abbassa la puntata al minimo vero ed esegue spin verificati.",
    )
    parser.add_argument("--device", required=True, help="ID del dispositivo ADB.")
    parser.add_argument("--slot", required=True, help="Nome della slot (profilo/memoria).")
    parser.add_argument("--target", type=float, required=True, help="Importo target da giocare (EUR).")
    parser.add_argument("--max-steps", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true", help="Non esegue tap reali (comportamento di default).")
    parser.add_argument("--live", action="store_true", help="Disattiva il dry-run: esegue tap reali.")
    parser.add_argument("--no-gpt", action="store_true", help="Disabilita il fallback GPT-4o-mini Vision.")
    parser.add_argument("--require-profile", action="store_true", help="Rifiuta le slot senza profilo in provider_profiles/.")
    parser.add_argument("--no-hitl", action="store_true", help="Disabilita il fallback HITL da terminale.")
    return parser.parse_args(argv)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()

    build_result = build_autopilot(args)
    if not build_result.success:
        logger.error("Impossibile inizializzare SlotAutoPilot: %s (%s)", build_result.error, build_result.error_code)
        sys.exit(1)
    autopilot = build_result.data

    exit_code = 1
    try:
        exit_code = _run_session(autopilot, args)
    except KeyboardInterrupt:
        logger.warning("Interrotto dall'operatore (KeyboardInterrupt).")
        exit_code = 1
    finally:
        shutdown_result = autopilot.shutdown(args.device)
        if not shutdown_result.success:
            logger.error("shutdown() fallito: %s (%s)", shutdown_result.error, shutdown_result.error_code)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
