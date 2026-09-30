import argparse
import logging
import os
import sys
import time
import warnings
from datetime import datetime
from decimal import Decimal

import openai

from orchestrator.core.result import Result
from orchestrator.modules.decision_engine.gpt_client import GPTDecisionClient
from orchestrator.modules.provider_profile.loader import ProviderProfileLoader
from orchestrator.modules.provider_profile.profile_builder import ProfileBuilder
from orchestrator.modules.screen_reader.reader import ScreenReader
from orchestrator.modules.slot_autopilot.autopilot import SlotAutoPilot
from orchestrator.modules.slot_autopilot.bet_controller import BetController
from orchestrator.modules.slot_autopilot.bet_menu import BetMenuSelector
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
from orchestrator.modules.slot_autopilot.screen_recovery import ScreenRecovery
from orchestrator.modules.slot_autopilot.verifier import ActionVerifier
from orchestrator.modules.slot_ocr.reader import SlotOCRReader
from orchestrator.modules.slot_ocr.vision_reader import ResilientOCRReader, VisionAmountReader
from orchestrator.modules.step_logger.logger import JSONLStepLogger
from orchestrator.modules.touch_injector.injector import TouchInjector

logger = logging.getLogger(__name__)

# Spin senza effetto di fila dopo i quali ci si ferma (invece di consumare --max-steps).
_MAX_CONSECUTIVE_NO_EFFECT_SPINS = 3
# Schermo non libero dopo uno spin (vincita/bonus/animazione lunga): si aspetta invece di
# chiudere la sessione, fino a questo tempo.
_MAX_BUSY_WAIT_SECONDS = 120
_BUSY_POLL_SECONDS = 5


# Loggers i cui messaggi INFO restano anche sulla console (progresso e eventi chiave);
# tutto il resto va solo su file (a meno che non sia un avviso/errore).
_CONSOLE_INFO_LOGGERS = ("__main__", "orchestrator.scripts.run_autopilot")
_HARMLESS_WARNING_PATTERNS = (".*pin_memory.*", ".*quantize_per_tensor.*")


class _ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name in _CONSOLE_INFO_LOGGERS


def _configure_logging(log_dir: str, slot_name: str) -> str:
    """Dettaglio completo (httpx, risposte GPT, ogni tocco) su file; sulla console solo
    la riga di progresso, gli eventi chiave e gli avvisi/errori. Ritorna il percorso del
    file. Le UserWarning innocue di torch (CPU senza acceleratore) non si mostrano."""
    os.makedirs(log_dir, exist_ok=True)
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slot_name.strip().lower()) or "slot"
    path = os.path.join(log_dir, f"autopilot_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    console.addFilter(_ConsoleFilter())
    root.addHandler(file_handler)
    root.addHandler(console)

    for pattern in _HARMLESS_WARNING_PATTERNS:
        warnings.filterwarnings("ignore", message=pattern)
    logging.getLogger("easyocr").setLevel(logging.ERROR)  # "Using CPU": informativo, una volta
    return path


_BET_FAILURE_MESSAGES = {
    "BET_UNREADABLE": "Non riesco a leggere la puntata: non tocco nulla (e non spinno).",
    "MONEY_MOVED": "Due tocchi durante la messa a punto della puntata hanno giocato uno spin: mi fermo.",
    "MIN_NOT_PROVEN": "Non riesco a dimostrare che questa sia la puntata minima: per sicurezza non spinno.",
    "ELEMENT_NOT_LOCATABLE": "Non trovo nessun modo di abbassare la puntata (ne' il tasto meno ne' il menu): mi fermo.",
    "STUCK_ON_UNEXPECTED_SCREEN": "Sono rimasto su una schermata inattesa e non riesco a tornare indietro: mi fermo.",
}


def _progress_line(spin_index: int, spin_result, total_played: Decimal, target: Decimal, seconds: float) -> str:
    bet = f"{spin_result.bet_amount:.2f}" if spin_result.bet_amount is not None else "-"
    balance = f"{spin_result.balance_after}" if spin_result.balance_after is not None else "-"
    win = f"{spin_result.win_amount:.2f}" if spin_result.win_amount else "0.00"
    return (
        f"Spin {spin_index} | puntata {bet} | saldo {balance} | vincita {win} | "
        f"giocato {total_played:.2f}/{target:.2f} | {seconds:.0f} s"
    )


def _check_openai_key(gpt_client) -> Result[bool]:
    """Controllo gratuito (elenco modelli, nessun token) che la chiave sia
    accettata: una chiave rifiutata fa fallire in silenzio ogni chiamata GPT e
    lascia solo l'operatore. Solo un rifiuto esplicito (401) e' bloccante: rete
    o server momentaneamente giu' non devono impedire di partire."""
    try:
        gpt_client.client.models.list()
    except openai.AuthenticationError as exc:
        return Result.fail(
            "La chiave OpenAI e' stata rifiutata (401 Unauthorized). Imposta una chiave valida "
            "(PowerShell: $env:OPENAI_API_KEY=\"sk-...\"; verificala su platform.openai.com/api-keys) "
            f"oppure rilancia con --no-gpt. Dettaglio: {exc}",
            "OPENAI_KEY_REJECTED",
        )
    except openai.OpenAIError as exc:
        logger.warning("Controllo chiave OpenAI non riuscito (%s): proseguo comunque.", exc)
    return Result.ok(True)


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
        except ValueError as exc:
            logger.warning("GPT fallback disabilitato (OPENAI_API_KEY mancante?): %s", exc)
        else:
            key_check = _check_openai_key(gpt_client)
            if not key_check.success:
                return Result.fail(key_check.error, key_check.error_code)
            strategies["gpt"] = GPTLocatorStrategy(gpt_client)
            # Rete di sicurezza per saldo e puntata: se l'OCR non li legge o li legge male (es. "50000.00"
            # letto 50.0) si usa la visione; dove l'OCR basta non costa nulla.
            ocr_reader = ResilientOCRReader(ocr_reader, VisionAmountReader(gpt_client))

    if config.enable_hitl_fallback:
        strategies["hitl"] = HITLStrategy(
            screen_reader, ocr_reader, touch_injector, describer=strategies.get("gpt"),
        )

    locator = ElementLocator(strategies, memory_dir=config.slot_memory_dir)
    verifier = ActionVerifier(touch_injector, screen_reader, ocr_reader, config)
    screen_recovery = ScreenRecovery(locator, verifier, config)
    bet_controller = BetController(
        locator, verifier, step_logger, config,
        menu_selector=BetMenuSelector(locator, verifier, screen_recovery=screen_recovery),
        screen_recovery=screen_recovery,
    )
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
        logger.error(
            "Impossibile verificare la puntata minima (%s): %s", reason,
            _BET_FAILURE_MESSAGES.get(reason, "mi fermo senza spinnare."),
        )
        return 1
    logger.info(
        "Puntata minima verificata: %s EUR (outcome=%s, tentativi=%d)",
        bet_result.data.final_bet, bet_result.data.outcome, bet_result.data.attempts,
    )

    target = Decimal(str(args.target))
    total_played = Decimal("0.00")
    spin_index = 0
    consecutive_no_effect = 0
    stopped_without_effect = False
    busy_since = None
    while total_played < target and spin_index < args.max_steps:
        spin_index += 1
        started = time.monotonic()
        spin_result = autopilot.spin(args.device, spin_index)
        seconds = time.monotonic() - started
        if not spin_result.success:
            logger.error("spin() fallito: %s (%s)", spin_result.error, spin_result.error_code)
            return 1
        if spin_result.data.bet_understated:
            logger.error(
                "La puntata reale sembra piu' alta di %s (il saldo e' sceso molto piu' del previsto): mi fermo.",
                spin_result.data.bet_amount,
            )
            stopped_without_effect = True
            break
        if spin_result.data.outcome != "blocked":
            busy_since = None
        if spin_result.data.outcome == "no_effect":
            consecutive_no_effect += 1
            if consecutive_no_effect >= _MAX_CONSECUTIVE_NO_EFFECT_SPINS:
                logger.error(
                    "%d spin di fila senza effetto: il tasto spin non e' stato trovato. Mi fermo "
                    "(rilancia: il punto sbagliato viene ricordato solo per la sessione).",
                    consecutive_no_effect,
                )
                stopped_without_effect = True
                break
        else:
            consecutive_no_effect = 0
        if spin_result.data.outcome == "success" and spin_result.data.bet_amount:
            total_played += spin_result.data.bet_amount
            logger.info("%s", _progress_line(spin_index, spin_result.data, total_played, target, seconds))
        elif spin_result.data.outcome == "blocked":
            if spin_result.data.violated_invariant == "UNSAFE_SCREEN_STATE":
                if busy_since is None:
                    busy_since = time.monotonic()
                    logger.info(
                        "Schermo occupato (vincita/bonus in corso?): aspetto che finisca (al massimo %d s)...",
                        _MAX_BUSY_WAIT_SECONDS,
                    )
                if time.monotonic() - busy_since < _MAX_BUSY_WAIT_SECONDS:
                    time.sleep(_BUSY_POLL_SECONDS)
                    spin_index -= 1   # l'attesa non consuma --max-steps
                    continue
                logger.warning("Lo schermo e' rimasto occupato per oltre %d s: mi fermo.", _MAX_BUSY_WAIT_SECONDS)
                stopped_without_effect = True
            else:
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
    return 1 if stopped_without_effect else 0


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
    parser.add_argument("--log-dir", default="logs", help="Cartella del file di log completo (default: logs).")
    parser.add_argument("--no-hitl", action="store_true", help="Disabilita il fallback HITL da terminale.")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    log_path = _configure_logging(getattr(args, "log_dir", "logs"), args.slot)
    logger.info("Dettaglio completo su file: %s", log_path)

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
